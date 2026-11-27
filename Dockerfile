FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PORT=10000 \
    HOME=/home/user

RUN apt-get update -qq && apt-get install -y -qq \
    mono-complete \
    xvfb \
    mariadb-server \
    mariadb-client \
    python3 \
    python3-requests \
    curl \
    iproute2 \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user && \
    mkdir -p /app /home/user/mysqldata /home/user/data /tmp/pkm && \
    chown -R user:user /app /home/user /tmp/pkm

WORKDIR /app

COPY --chown=user:user cloud_proxy.py /app/cloud_proxy.py
COPY --chown=user:user backup_sync.py /app/backup_sync.py
COPY --chown=user:user HeadlessContainer.cs /app/HeadlessContainer.cs
COPY --chown=user:user start_cloud.sh /app/start_cloud.sh

RUN sed -i 's/\r$//' /app/start_cloud.sh /app/cloud_proxy.py /app/backup_sync.py /app/HeadlessContainer.cs && \
    chmod +x /app/start_cloud.sh

USER user

EXPOSE 10000

CMD ["bash", "/app/start_cloud.sh"]
