# CPU image for the API and logical-mode workers (BRD 7.9).
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SIMCORE_DATA=/data SIMCORE_EXAMPLES=/app/examples
COPY pyproject.toml README.md ./
COPY src ./src
COPY domains ./domains
COPY services ./services
COPY schemas ./schemas
COPY examples ./examples
RUN pip install --no-cache-dir ".[redis,service]"
EXPOSE 8000
CMD ["python", "-m", "services.api"]
