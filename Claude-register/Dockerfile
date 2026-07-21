FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY claude_register ./claude_register
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /app/runtime \
    && chown app:app /app/runtime

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

CMD ["uvicorn", "claude_register.presentation.web:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
