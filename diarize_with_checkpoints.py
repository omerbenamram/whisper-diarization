import argparse
import os
import wget
from omegaconf import OmegaConf
import json
import shutil
from faster_whisper import WhisperModel
import whisperx
import torch
import librosa
import soundfile
import pickle
import shutil
from nemo.collections.asr.models.msdd_models import NeuralDiarizer
from deepmultilingualpunctuation import PunctuationModel
from collections import defaultdict
import re
import logging
import subprocess
import shlex
from pydub import AudioSegment

from nemo.collections.asr.models.label_models import EncDecSpeakerLabelModel

parser = argparse.ArgumentParser()
parser.add_argument(
    "-a", "--audio", help="name of the target audio file", required=True
)
parser.add_argument(
    "--no-stem",
    action="store_false",
    dest="stemming",
    default=True,
    help="Disables source separation."
    "This helps with long files that don't contain a lot of music.",
)

parser.add_argument(
    "--whisper-model",
    dest="whisper_model",
    default="medium.en",
    help="name of the Whisper model to use",
)

parser.add_argument(
    "--device",
    dest="device",
    default="cuda" if torch.cuda.is_available() else "cpu",
    help="if you have a GPU use 'cuda', otherwise 'cpu'",
)

parser.add_argument(
    "--num-speakers",
    dest="num_speakers",
    type=int,
    default=-1,
    help="number of speakers in the audio, default is -1 which means the model will infer the number of speakers",
)

args = parser.parse_args()

punct_model_langs = [
    "en",
    "fr",
    "de",
    "es",
    "it",
    "nl",
    "pt",
    "bg",
    "pl",
    "cs",
    "sk",
    "sl",
]
wav2vec2_langs = [
    "en",
    "fr",
    "de",
    "es",
    "it",
    "nl",
    "pt",
    "ja",
    "zh",
    "uk",
    "pt",
    "ar",
    "ru",
    "pl",
    "hu",
    "fi",
    "fa",
    "el",
    "tr",
]


def create_config(output_dir):
    DOMAIN_TYPE = "telephonic"  # Can be meeting or telephonic based on domain type of the audio file
    CONFIG_FILE_NAME = f"diar_infer_{DOMAIN_TYPE}.yaml"
    CONFIG_URL = f"https://raw.githubusercontent.com/NVIDIA/NeMo/main/examples/speaker_tasks/diarization/conf/inference/{CONFIG_FILE_NAME}"
    MODEL_CONFIG = os.path.join(output_dir, CONFIG_FILE_NAME)
    if not os.path.exists(MODEL_CONFIG):
        MODEL_CONFIG = wget.download(CONFIG_URL, output_dir)

    config = OmegaConf.load(MODEL_CONFIG)

    data_dir = os.path.join(output_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    config.batch_size = 64
    meta = {
        "audio_filepath": os.path.join(output_dir, "mono_file.wav"),
        "offset": 0,
        "duration": None,
        "label": "infer",
        "text": "-",
        "rttm_filepath": None,
        "uem_filepath": None,
    }

    if args.num_speakers > 0:
        meta["num_speakers"] = args.num_speakers

    with open(os.path.join(data_dir, "input_manifest.json"), "w") as fp:
        json.dump(meta, fp)
        fp.write("\n")

    pretrained_vad = "vad_multilingual_marblenet"
    pretrained_speaker_model = "titanet_large" #"ecapa_tdnn "

    config.num_workers = 1  # Workaround for multiprocessing hanging with ipython issue

    config.diarizer.manifest_filepath = os.path.join(data_dir, "input_manifest.json")
    config.diarizer.out_dir = (
        output_dir  # Directory to store intermediate files and prediction outputs
    )

    config.diarizer.speaker_embeddings.model_path = pretrained_speaker_model
    config.diarizer.oracle_vad = (
        False  # compute VAD provided with model_path to vad config
    )

    if args.num_speakers > 0:
        config.diarizer.clustering.parameters.oracle_num_speakers = True

    # Here, we use our in-house pretrained NeMo VAD model
    config.diarizer.vad.model_path = pretrained_vad
    config.diarizer.vad.parameters.onset = 0.8
    config.diarizer.vad.parameters.offset = 0.6
    config.diarizer.vad.parameters.pad_offset = -0.05
    config.diarizer.msdd_model.model_path = (
        "diar_msdd_telephonic"  # Telephonic speaker diarization model
    )

    return config


def get_word_ts_anchor(s, e, option="start"):
    if option == "end":
        return e
    elif option == "mid":
        return (s + e) / 2
    return s


def get_words_speaker_mapping(wrd_ts, spk_ts, word_anchor_option="start"):
    s, e, sp = spk_ts[0]
    wrd_pos, turn_idx = 0, 0
    wrd_spk_mapping = []
    for wrd_dict in wrd_ts:
        ws, we, wrd = (
            int(wrd_dict["start"] * 1000),
            int(wrd_dict["end"] * 1000),
            wrd_dict["text"],
        )
        wrd_pos = get_word_ts_anchor(ws, we, word_anchor_option)
        while wrd_pos > float(e):
            turn_idx += 1
            turn_idx = min(turn_idx, len(spk_ts) - 1)
            s, e, sp = spk_ts[turn_idx]
            if turn_idx == len(spk_ts) - 1:
                e = get_word_ts_anchor(ws, we, option="end")
        wrd_spk_mapping.append(
            {"word": wrd, "start_time": ws, "end_time": we, "speaker": sp}
        )
    return wrd_spk_mapping


sentence_ending_punctuations = ".?!"


def get_first_word_idx_of_sentence(word_idx, word_list, speaker_list, max_words):
    is_word_sentence_end = (
        lambda x: x >= 0 and word_list[x][-1] in sentence_ending_punctuations
    )
    left_idx = word_idx
    while (
        left_idx > 0
        and word_idx - left_idx < max_words
        and speaker_list[left_idx - 1] == speaker_list[left_idx]
        and not is_word_sentence_end(left_idx - 1)
    ):
        left_idx -= 1

    return left_idx if left_idx == 0 or is_word_sentence_end(left_idx - 1) else -1


def get_last_word_idx_of_sentence(word_idx, word_list, max_words):
    is_word_sentence_end = (
        lambda x: x >= 0 and word_list[x][-1] in sentence_ending_punctuations
    )
    right_idx = word_idx
    while (
        right_idx < len(word_list)
        and right_idx - word_idx < max_words
        and not is_word_sentence_end(right_idx)
    ):
        right_idx += 1

    return (
        right_idx
        if right_idx == len(word_list) - 1 or is_word_sentence_end(right_idx)
        else -1
    )


def get_realigned_ws_mapping_with_punctuation(
    word_speaker_mapping, max_words_in_sentence=50
):
    is_word_sentence_end = (
        lambda x: x >= 0
        and word_speaker_mapping[x]["word"][-1] in sentence_ending_punctuations
    )
    wsp_len = len(word_speaker_mapping)

    words_list, speaker_list = [], []
    for k, line_dict in enumerate(word_speaker_mapping):
        word, speaker = line_dict["word"], line_dict["speaker"]
        words_list.append(word)
        speaker_list.append(speaker)

    k = 0
    while k < len(word_speaker_mapping):
        line_dict = word_speaker_mapping[k]
        if (
            k < wsp_len - 1
            and speaker_list[k] != speaker_list[k + 1]
            and not is_word_sentence_end(k)
        ):
            left_idx = get_first_word_idx_of_sentence(
                k, words_list, speaker_list, max_words_in_sentence
            )
            right_idx = (
                get_last_word_idx_of_sentence(
                    k, words_list, max_words_in_sentence - k + left_idx - 1
                )
                if left_idx > -1
                else -1
            )
            if min(left_idx, right_idx) == -1:
                k += 1
                continue

            spk_labels = speaker_list[left_idx : right_idx + 1]
            mod_speaker = max(set(spk_labels), key=spk_labels.count)
            if spk_labels.count(mod_speaker) < len(spk_labels) // 2:
                k += 1
                continue

            speaker_list[left_idx : right_idx + 1] = [mod_speaker] * (
                right_idx - left_idx + 1
            )
            k = right_idx

        k += 1

    k, realigned_list = 0, []
    while k < len(word_speaker_mapping):
        line_dict = word_speaker_mapping[k].copy()
        line_dict["speaker"] = speaker_list[k]
        realigned_list.append(line_dict)
        k += 1

    return realigned_list


def get_sentences_speaker_mapping(word_speaker_mapping, spk_ts):
    s, e, spk = spk_ts[0]
    prev_spk = spk

    snts = []
    snt = {"speaker": f"Speaker {spk}", "start_time": s, "end_time": e, "text": ""}

    for wrd_dict in word_speaker_mapping:
        wrd, spk = wrd_dict["word"], wrd_dict["speaker"]
        s, e = wrd_dict["start_time"], wrd_dict["end_time"]
        if spk != prev_spk:
            snts.append(snt)
            snt = {
                "speaker": f"Speaker {spk}",
                "start_time": s,
                "end_time": e,
                "text": "",
            }
        else:
            snt["end_time"] = e
        snt["text"] += wrd + " "
        prev_spk = spk

    snts.append(snt)
    return snts


def get_speaker_aware_transcript(sentences_speaker_mapping, f):
    for sentence_dict in sentences_speaker_mapping:
        sp = sentence_dict["speaker"]
        text = sentence_dict["text"]
        f.write(f"\n\n{sp}: {text}")


def format_timestamp(
    milliseconds: float, always_include_hours: bool = False, decimal_marker: str = "."
):
    assert milliseconds >= 0, "non-negative timestamp expected"

    hours = milliseconds // 3_600_000
    milliseconds -= hours * 3_600_000

    minutes = milliseconds // 60_000
    milliseconds -= minutes * 60_000

    seconds = milliseconds // 1_000
    milliseconds -= seconds * 1_000

    hours_marker = f"{hours:02d}:" if always_include_hours or hours > 0 else ""
    return (
        f"{hours_marker}{minutes:02d}:{seconds:02d}{decimal_marker}{milliseconds:03d}"
    )


def write_srt(transcript, file):
    """
    Write a transcript to a file in SRT format.

    """
    for i, segment in enumerate(transcript, start=1):
        # write srt lines
        print(
            f"{i}\n"
            f"{format_timestamp(segment['start_time'], always_include_hours=True, decimal_marker=',')} --> "
            f"{format_timestamp(segment['end_time'], always_include_hours=True, decimal_marker=',')}\n"
            f"{segment['speaker']}: {segment['text'].strip().replace('-->', '->')}\n",
            file=file,
            flush=True,
        )


def cleanup(path: str):
    """path could either be relative or absolute."""
    # check if file or directory exists
    if os.path.isfile(path) or os.path.islink(path):
        # remove file
        os.remove(path)
    elif os.path.isdir(path):
        # remove directory and all its content
        shutil.rmtree(path)
    else:
        raise ValueError("Path {} is not a file or dir.".format(path))

# %% [markdown]
# # Options

# %%
# Name of the audio file
audio_path = args.audio

# Whether to enable music removal from speech, helps increase diarization quality but uses alot of ram
enable_stemming = args.stemming

# (choose from 'tiny.en', 'tiny', 'base.en', 'base', 'small.en', 'small', 'medium.en', 'medium', 'large-v1', 'large-v2', 'large')
whisper_model_name = args.whisper_model

# %%
ROOT = os.getcwd()
temp_path = os.path.join(ROOT, "temp_outputs")
os.makedirs(temp_path, exist_ok=True)

# %% [markdown]
# # Processing

# %% [markdown]
# ## Separating music from speech using Demucs
#
# ---
#
# By isolating the vocals from the rest of the audio, it becomes easier to identify and track individual speakers based on the spectral and temporal characteristics of their speech signals. Source separation is just one of many techniques that can be used as a preprocessing step to help improve the accuracy and reliability of the overall diarization process.

# %%
checkpoint = os.path.join(temp_path, "htdemucs", os.path.splitext(os.path.split(audio_path)[-1])[0], "vocals.wav")

if not os.path.exists(checkpoint):
    if enable_stemming:
        print("Running demucs.sparate")
        # Isolate vocals from the rest of the audio

        return_code = os.system(
            f'python3 -m demucs.separate -n htdemucs --two-stems=vocals "{audio_path}" -o "temp_outputs"'
        )

        if return_code != 0:
            logging.warning(
                "Source splitting failed, using original audio file."
            )
            vocal_target = audio_path
        else:
            vocal_target = checkpoint
    else:
        print("Stemming disabled - skipping demucs.sparate")
        vocal_target = audio_path
else:
    print(f"Using existing demucs.sparate output from {checkpoint}")
    vocal_target = checkpoint

# %% [markdown]
# ## Transcriping audio using Whisper and realligning timestamps using Wav2Vec2
# ---
# This code uses two different open-source models to transcribe speech and perform forced alignment on the resulting transcription.
#
# The first model is called OpenAI Whisper, which is a speech recognition model that can transcribe speech with high accuracy. The code loads the whisper model and uses it to transcribe the vocal_target file.
#
# The output of the transcription process is a set of text segments with corresponding timestamps indicating when each segment was spoken.
#

# %%
checkpoint = os.path.join(temp_path, "whisper_results.pkl")

if os.path.exists(checkpoint):
    print("Loading whisper results from checkpoint")
    whisper_results = pickle.load(open(checkpoint, "rb"))
    info = pickle.load(open(os.path.join(temp_path, "whisper_info.pkl"), "rb"))

else:
    print("Running whisper")
    # Run on GPU with FP16
    whisper_model = WhisperModel(whisper_model_name, device="cuda", compute_type="float16")

    # or run on GPU with INT8
    # model = WhisperModel(model_size, device="cuda", compute_type="int8_float16")
    # or run on CPU with INT8
    # model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments, info = whisper_model.transcribe(
        vocal_target, beam_size=1, word_timestamps=True
    )
    whisper_results = []
    for segment in segments:
        whisper_results.append(segment._asdict())

    # save results
    pickle.dump(whisper_results, open(checkpoint, "wb"))
    pickle.dump(info, open(os.path.join(temp_path, "whisper_info.pkl"), "wb"))

    # clear gpu vram
    del whisper_model
    torch.cuda.empty_cache()

# %% [markdown]
# ## Aligning the transcription with the original audio using Wav2Vec2
# ---
# The second model used is called wav2vec2, which is a large-scale neural network that is designed to learn representations of speech that are useful for a variety of speech processing tasks, including speech recognition and alignment.
#
# The code loads the wav2vec2 alignment model and uses it to align the transcription segments with the original audio signal contained in the vocal_target file. This process involves finding the exact timestamps in the audio signal where each segment was spoken and aligning the text accordingly.
#
# By combining the outputs of the two models, the code produces a fully aligned transcription of the speech contained in the vocal_target file. This aligned transcription can be useful for a variety of speech processing tasks, such as speaker diarization, sentiment analysis, and language identification.
#
# If there's no Wav2Vec2 model available for your language, word timestamps generated by whisper will be used instead.

# %%
checkpoint = os.path.join(temp_path, "wave2vec2.pkl")
if os.path.exists(checkpoint):
    print("Loading wave2vec2 results from checkpoint")
    word_timestamps = pickle.load(open(checkpoint, "rb"))

else:
    if info.language in wav2vec2_langs:
        device = "cuda"
        alignment_model, metadata = whisperx.load_align_model(
            language_code=info.language, device=device
        )
        result_aligned = whisperx.align(
            whisper_results, alignment_model, metadata, vocal_target, device
        )
        word_timestamps = result_aligned["word_segments"]
        # clear gpu vram
        del alignment_model
        torch.cuda.empty_cache()
    else:
        word_timestamps = []
        for segment in whisper_results:
            for word in segment["words"]:
                word_timestamps.append({"text": word[2], "start": word[0], "end": word[1]})

    pickle.dump(word_timestamps, open(checkpoint, "wb"))

# %% [markdown]
# ## Convert audio to mono for NeMo combatibility

# %%
checkpoint = os.path.join(temp_path, "mono_file.wav")

if os.path.exists(checkpoint):
    print(f"{checkpoint} mono file exists")
else:
    signal, sample_rate = librosa.load(vocal_target, sr=None)
    soundfile.write(checkpoint, signal, sample_rate, "PCM_24")

# %% [markdown]
# ## Speaker Diarization using NeMo MSDD Model
# ---
# This code uses a model called Nvidia NeMo MSDD (Multi-scale Diarization Decoder) to perform speaker diarization on an audio signal. Speaker diarization is the process of separating an audio signal into different segments based on who is speaking at any given time.

# %%
checkpoint = os.path.join(os.path.join(temp_path, "pred_rttms"), "mono_file.rttm")

if not os.path.exists(checkpoint):
    print(f"{checkpoint} rttm file does not exist, running diarization")
    # Initialize NeMo MSDD diarization model
    msdd_model = NeuralDiarizer(cfg=create_config(temp_path)).to("cuda")
    msdd_model.diarize()

    del msdd_model
    torch.cuda.empty_cache()
else:
    print(f"{checkpoint} exists, skipping diarization")

embeddings_model = EncDecSpeakerLabelModel.from_pretrained("titanet_large")
identified_model = pickle.load(open("grey_vs_brady.pkl", "rb"))

# %%
checkpoint = os.path.join(temp_path, "segments_dir")
os.makedirs(checkpoint, exist_ok=True)

# number of segments to use to identify speakers.
n_segments = 10

# Read the diarization output
with open(f'{temp_path}/pred_rttms/mono_file.rttm', 'r') as f:
    lines = f.readlines()


if not os.path.exists(os.path.join(temp_path, "mono_file_5_minutes.wav")):
    subprocess.run(shlex.split("ffmpeg -i temp_outputs/mono_file.wav -ss 00:00:00 -t 00:05:00 -c copy temp_outputs/mono_file_5_minutes.wav"))

# Load the audio file
audio = AudioSegment.from_wav('temp_outputs/mono_file_5_minutes.wav')

# Sample rate of the audio file
sample_rate = soundfile.info('temp_outputs/mono_file_5_minutes.wav').samplerate

# List to store the manifest data
manifest_data = []

# speaker_0 -> [result_1, result_2, ...]
# we will take majority to identify the speaker
speaker_to_identity = defaultdict(list)
speaker_to_sample_counter = defaultdict(int)

for line in lines:
    fields = line.split()
    speaker = fields[7]
    start_time = float(fields[3]) * 1000  # convert to milliseconds
    end_time = start_time + (float(fields[4]) * 1000)  # convert to milliseconds

    duration = (end_time - start_time) / 1000  # convert back to seconds

    # discard samples less than 1 second
    if duration < 1.0:
        continue

    speaker_to_sample_counter[speaker] += 1

    if speaker_to_sample_counter[speaker] >= n_segments:
        print(f"Speaker {speaker} has enough samples, skipping")
        continue

    # Slice the audio segment
    segment = audio[start_time:end_time]

    # Save the audio segment
    segment_path = f"{checkpoint}/{speaker}_{start_time}_{end_time}.wav"
    segment.export(segment_path, format='wav')
    print(f"Processing {segment_path}")

    # Load the audio segment
    embeddings = embeddings_model.get_embedding(segment_path)

    # Identify the speaker
    result = identified_model.predict(embeddings.cpu().numpy())
    result = result[0]
    if result == 0:
        identity = "Brady"
    else:
        identity = "Grey"

    speaker_to_identity[speaker].append(identity)


# select majority for each key
speaker_to_identity = {k: max(set(v), key=v.count) for k, v in speaker_to_identity.items()}

new_rttm_lines = []
for line in lines:
    # replace the speaker id with the identity
    speaker = line.split()[7]
    new_rttm_lines.append(line.replace(speaker, speaker_to_identity[speaker]))

# write the new rttm file
with open(f'{temp_path}/pred_rttms/mono_file.rttm', 'w') as f:
    f.writelines(new_rttm_lines)


# %% [markdown]
# ## Mapping Spekers to Sentences According to Timestamps

# %%
# Reading timestamps <> Speaker Labels mapping

speaker_ts = []
with open(os.path.join(temp_path, "pred_rttms", "mono_file.rttm"), "r") as f:
    lines = f.readlines()
    for line in lines:
        line_list = line.split(" ")
        s = int(float(line_list[5]) * 1000)
        e = s + int(float(line_list[8]) * 1000)
        speaker_ts.append([s, e, line_list[11]])

wsm = get_words_speaker_mapping(word_timestamps, speaker_ts, "start")

# %% [markdown]
# ## Realligning Speech segments using Punctuation
# ---
#
# This code provides a method for disambiguating speaker labels in cases where a sentence is split between two different speakers. It uses punctuation markings to determine the dominant speaker for each sentence in the transcription.
#
# ```
# Speaker A: It's got to come from somewhere else. Yeah, that one's also fun because you know the lows are
# Speaker B: going to suck, right? So it's actually it hits you on both sides.
# ```
#
# For example, if a sentence is split between two speakers, the code takes the mode of speaker labels for each word in the sentence, and uses that speaker label for the whole sentence. This can help to improve the accuracy of speaker diarization, especially in cases where the Whisper model may not take fine utterances like "hmm" and "yeah" into account, but the Diarization Model (Nemo) may include them, leading to inconsistent results.
#
# The code also handles cases where one speaker is giving a monologue while other speakers are making occasional comments in the background. It ignores the comments and assigns the entire monologue to the speaker who is speaking the majority of the time. This provides a robust and reliable method for realigning speech segments to their respective speakers based on punctuation in the transcription.

# %%
if info.language in punct_model_langs:
    # restoring punctuation in the transcript to help realign the sentences
    punct_model = PunctuationModel(model="kredor/punctuate-all")

    words_list = list(map(lambda x: x["word"], wsm))

    labled_words = punct_model.predict(words_list)

    ending_puncts = ".?!"
    model_puncts = ".,;:!?"

    # We don't want to punctuate U.S.A. with a period. Right?
    is_acronym = lambda x: re.fullmatch(r"\b(?:[a-zA-Z]\.){2,}", x)

    for word_dict, labeled_tuple in zip(wsm, labled_words):
        word = word_dict["word"]
        if (
            word
            and labeled_tuple[1] in ending_puncts
            and (word[-1] not in model_puncts or is_acronym(word))
        ):
            word += labeled_tuple[1]
            if word.endswith(".."):
                word = word.rstrip(".")
            word_dict["word"] = word



    wsm = get_realigned_ws_mapping_with_punctuation(wsm)
else:
    print(
        f'Punctuation restoration is not available for {whisper_results["language"]} language.'
    )

ssm = get_sentences_speaker_mapping(wsm, speaker_ts)

# %%
vocal_model_data_path = os.path.join("vocal_model_data")
os.makedirs(vocal_model_data_path, exist_ok=True)

# copy vocal_target to vocal_model_data
filename = os.path.splitext(os.path.split(audio_path)[-1])[0]
shutil.copyfile(
    os.path.join(temp_path, "mono_file.wav"),
    os.path.join(vocal_model_data_path, f"vocals_{filename}.wav"),
)

# copy the rttms
shutil.copyfile(
    os.path.join(temp_path, "pred_rttms", "mono_file.rttm"),
    os.path.join(vocal_model_data_path, f"vocals_{filename}.rttm"),
)

# %% [markdown]
# ## Cleanup and Exporing the results

# %%
with open(f"{audio_path[:-4]}.txt", "w", encoding="utf-8-sig") as f:
    get_speaker_aware_transcript(ssm, f)

with open(f"{audio_path[:-4]}.srt", "w", encoding="utf-8-sig") as srt:
    write_srt(ssm, srt)

cleanup(temp_path)
print("Done!")
