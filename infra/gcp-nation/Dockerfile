FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for cryptography and MySQL client bindings.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        default-libmysqlclient-dev \
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Install Litestream for SQLite → GCS continuous replication.
ARG LITESTREAM_VERSION=v0.3.13
RUN wget -q "https://github.com/benbjohnson/litestream/releases/download/${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-amd64.tar.gz" -O /tmp/litestream.tar.gz && \
    tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin && \
    rm /tmp/litestream.tar.gz

# Install runtime dependencies from pinned requirements.
COPY syncbot/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application code.
COPY syncbot/ ./syncbot/

# Copy Litestream config and entrypoint.
COPY infra/gcp-nation/litestream.yml /etc/litestream.yml
COPY infra/gcp-nation/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Persistent data directory for the SQLite DB file.
RUN mkdir -p /data

WORKDIR /app/syncbot

# Cloud Run sets PORT (default 8080); local dev may use 3000.
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
