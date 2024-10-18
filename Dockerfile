FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_LINK_MODE=copy

WORKDIR /root

RUN apt-get update -y && apt-get install -y git build-essential clang python3-dev python3-pip python3-venv ffmpeg

ADD .python-version .
ADD pyproject.toml .
ADD requirements.txt .
RUN uv venv
RUN --mount=type=cache,target=/root/.cache/uv uv pip install setuptools cython pybind11 && uv pip install -r requirements.txt

COPY . .

ENTRYPOINT ["python3", "diarize_with_checkpoints.py"]