# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build
WORKDIR /build/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTH_REQUIRED=true \
    ASSISTANT_FILE_ROOT=/opt/luma/backend/data/files
WORKDIR /opt/luma/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 luma \
    && useradd --uid 10001 --gid luma --create-home luma
COPY --chown=luma:luma backend/ ./
COPY --from=web-build --chown=luma:luma /build/web/dist/ /opt/luma/apps/web/dist/
RUN mkdir -p /opt/luma/backend/data/files \
    && chown -R luma:luma /opt/luma/backend/data
USER luma
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import json, urllib.request; result = json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)); assert result['database']['reachable'] and result['redis']['reachable']"
# FastAPI lifespan runs database migrations under a PostgreSQL advisory lock.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers \"${WORKERS:-2}\""]
