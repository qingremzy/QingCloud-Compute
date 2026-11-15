FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QCLOUD_DB_PATH=/data/qingcloud.sqlite3

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 qingcloud
COPY qingcloud ./qingcloud
RUN mkdir -p /data && chown -R qingcloud:qingcloud /app /data
USER qingcloud

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "qingcloud.main:app", "--host", "0.0.0.0", "--port", "8000"]
