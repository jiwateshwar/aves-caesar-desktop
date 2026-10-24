# Aves Caesar workflow server. Build from the repository root:
#   docker build -f server/Dockerfile -t aves-caesar-workflow .
# For a CPU-only image (no NVIDIA GPU):
#   docker build -f server/Dockerfile --build-arg ONNXRUNTIME="onnxruntime>=1.20" -t aves-caesar-workflow:cpu .

FROM node:22-bookworm-slim AS web
WORKDIR /build/server/web
COPY server/web/package.json server/web/package-lock.json ./
RUN npm ci
COPY server/web/ ./
RUN npm run build


FROM python:3.12-slim-bookworm

# onnxruntime-gpu with these extras ships CUDA 12 and cuDNN 9 as Python wheels, so no CUDA base image is
# needed; the NVIDIA container runtime only has to provide the driver (enable it in TrueNAS Apps settings).
ARG ONNXRUNTIME="onnxruntime-gpu[cuda,cudnn]>=1.20"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libimage-exiftool-perl tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "numpy>=1.26" "Pillow>=10.0" "pillow-heif>=0.20" "gpxpy>=1.6" "$ONNXRUNTIME" \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" "sqlalchemy>=2.0.30" "psycopg[binary]>=3.2" \
        "reverse_geocode==1.6.6" "scipy>=1.13" "python-multipart>=0.0.9" "httpx>=0.27"

WORKDIR /app
# The engine resolves runtime_config.json relative to /app (three levels above bird_desktop/runtime).
COPY backend/bird_desktop /app/backend/bird_desktop
COPY runtime_config.json model-version.json /app/
COPY scripts/models/download_release_models.py /app/scripts/models/
COPY LICENSE.txt NOTICE.txt /app/
COPY licenses /app/licenses
COPY server/aves_server /app/server/aves_server
COPY --from=web /build/server/aves_server/static /app/server/aves_server/static
COPY server/docker/entrypoint.sh /usr/local/bin/aves-entrypoint

ENV PYTHONPATH=/app/server:/app/backend \
    PYTHONUNBUFFERED=1 \
    AVES_DATA_DIR=/data/appdata \
    AVES_MODELS_DIR=/data/appdata/models \
    AVES_DUMP_DIR=/data/photos/Dumpsite \
    AVES_PUBLISHED_DIR="/data/photos/Published Pics" \
    AVES_PROVIDER=auto \
    HOME=/tmp

RUN chmod 0755 /usr/local/bin/aves-entrypoint
EXPOSE 8080
ENTRYPOINT ["tini", "--", "aves-entrypoint"]
CMD ["web"]
