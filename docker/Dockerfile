# CPU image for the API (serving the SPA) and logical-mode workers (BRD 7.9).
FROM node:22-slim AS frontend
WORKDIR /build
COPY schemas ./schemas
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --no-audit --no-fund
COPY frontend ./frontend
RUN cd frontend && npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SIMCORE_DATA=/data \
    SIMCORE_EXAMPLES=/app/examples SIMCORE_FRONTEND=/app/frontend SIMCORE_HOST=0.0.0.0
COPY pyproject.toml README.md ./
COPY src ./src
COPY domains ./domains
COPY services ./services
COPY schemas ./schemas
COPY examples ./examples
RUN pip install --no-cache-dir ".[redis,service,report]"
COPY --from=frontend /build/frontend/dist ./frontend
EXPOSE 8000
CMD ["python", "-m", "services.api"]
