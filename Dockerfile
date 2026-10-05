FROM python:3.12-slim

LABEL org.opencontainers.image.title="filme-metadata"
LABEL org.opencontainers.image.description="Erkennt falsche Jellyfin-Metadaten und korrigiert die Provider-ID"
LABEL org.opencontainers.image.source="https://github.com/PaulG67/filme-metadata"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8792 \
    PUID=99 \
    PGID=100 \
    JELLYFIN_BASEURL=http://172.17.0.1:8096

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      gosu \
      passwd \
      ca-certificates \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh \
    && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8792

HEALTHCHECK --interval=60s --timeout=8s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8792'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=5).status==200 else 1)"

ENTRYPOINT ["/docker-entrypoint.sh"]
