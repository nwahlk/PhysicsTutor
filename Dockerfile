FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --uid 10001 --no-create-home appuser

COPY migrate.py ./
COPY migrations ./migrations
COPY data ./data
COPY tutor ./tutor

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"

CMD ["sh", "-c", "exec uvicorn tutor.api:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2} --timeout-graceful-shutdown 15"]
