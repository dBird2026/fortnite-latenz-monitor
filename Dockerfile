FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends traceroute tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor ./monitor

ENV FLM_DATA_DIR=/data \
    FLM_BIND=0.0.0.0 \
    FLM_BROWSER=0 \
    TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=5)" || exit 1

LABEL org.opencontainers.image.title="Fortnite-Latenz-Monitor" \
      org.opencontainers.image.description="Misst die Latenz zu den Fortnite-Servern (UDP 22222) alle 5 Minuten, mit Dashboard"

CMD ["python", "-m", "monitor"]
