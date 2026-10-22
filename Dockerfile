FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    VOICEFLOCK_DATA_DIR=/data HF_HOME=/data/models/hf TORCH_HOME=/data/models/torch

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# CPU-only torch keeps the image small. For NVIDIA GPUs see docker-compose.gpu.yml.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install torch torchaudio --index-url ${TORCH_INDEX} && pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY voiceflock ./voiceflock
RUN pip install --no-deps -e .

VOLUME ["/data"]
EXPOSE 8787
CMD ["voiceflock", "serve", "--host", "0.0.0.0", "--port", "8787"]
