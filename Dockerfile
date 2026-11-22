FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /app
RUN useradd --create-home --uid 10001 wwos
COPY pyproject.toml README.md ./
COPY wwos ./wwos
RUN pip install --no-cache-dir ".[postgres]" && pip uninstall -y pip setuptools wheel 2>/dev/null || true
USER wwos
EXPOSE 8000
# Cloud Run sets PORT; locally it defaults to 8000. Proxy headers are trusted only from
# FORWARDED_ALLOW_IPS (Cloud Run's front end: set to "*" there; 127.0.0.1 by default).
CMD ["sh", "-c", "exec uvicorn wwos.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --no-server-header"]
