FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Moscow
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tgnet ./tgnet
COPY config ./config

RUN useradd -m -u 10001 tgnet && mkdir -p /app/data && chown -R tgnet /app
USER tgnet

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/',timeout=5).status==200 else 1)"

CMD ["python", "-m", "tgnet", "run"]
