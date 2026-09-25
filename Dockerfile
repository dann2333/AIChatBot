ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM ${BASE_IMAGE}
# 沙箱内预装的命令行工具与 Node.js，可通过 --build-arg EXTRA_PACKAGES="ffmpeg imagemagick" 追加
ARG EXTRA_PACKAGES=""
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap ca-certificates tzdata busybox \
        curl wget git jq procps file tree less vim-tiny bc sqlite3 \
        zip unzip xz-utils bzip2 p7zip-full dnsutils iputils-ping netcat-openbsd \
        nodejs npm fonts-noto-cjk ${EXTRA_PACKAGES} \
    && for a in $(busybox --list); do command -v "$a" >/dev/null || ln -s /bin/busybox "/usr/local/bin/$a"; done \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY sandbox-requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/sandbox-requirements.txt && rm /tmp/sandbox-requirements.txt \
    && printf 'font.sans-serif: Noto Sans CJK SC, DejaVu Sans\naxes.unicode_minus: False\n' > /usr/local/etc/matplotlibrc
COPY main.py /app/
COPY aibot /app/aibot
# config.yaml、Chat.db、日志与沙箱持久目录都在 /data
WORKDIR /data
VOLUME /data
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/main.py"]
