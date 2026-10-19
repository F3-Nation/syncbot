# SyncBot Cloud Run image (SQLite + Litestream, also works for existing MySQL/TiDB).
# Build from the repository root:
#   docker build -f infra/gcp/Dockerfile --platform linux/amd64 .
FROM python:3.12-slim

WORKDIR /app

# System deps for cryptography, MySQL client bindings, and Litestream download.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        default-libmysqlclient-dev \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Litestream v0.3.13 linux-amd64 (SHA256 of the GitHub release tarball).
ARG LITESTREAM_VERSION=v0.3.13
ARG LITESTREAM_SHA256=eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0
RUN wget -q "https://github.com/benbjohnson/litestream/releases/download/${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-amd64.tar.gz" -O /tmp/litestream.tar.gz && \
    echo "${LITESTREAM_SHA256}  /tmp/litestream.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin && \
    rm /tmp/litestream.tar.gz

COPY syncbot/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY syncbot/ ./syncbot/
COPY infra/gcp/litestream.yml /etc/litestream.yml
COPY infra/gcp/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

WORKDIR /app/syncbot

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
