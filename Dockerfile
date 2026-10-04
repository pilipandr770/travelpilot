FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    UPLOAD_DIR=/data/uploads PDF_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 -m app && mkdir -p /data/uploads && chown -R app /data

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=app . .
RUN chmod +x docker/entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["web"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD curl -fsS http://127.0.0.1:8000/_internal/health || exit 1
