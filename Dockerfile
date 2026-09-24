ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM ${BASE_IMAGE}
# 沙箱内可用的命令行工具，可通过 --build-arg EXTRA_PACKAGES="nodejs ffmpeg" 追加
ARG EXTRA_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl git jq procps tzdata unzip zip ${EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY main.py /app/
COPY aibot /app/aibot
# config.yaml、Chat.db、日志与沙箱持久目录都在 /data
WORKDIR /data
VOLUME /data
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/main.py"]
