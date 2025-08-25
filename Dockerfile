# --- Dockerfile ---
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# сертификаты + tzdata, чтобы HTTPS и время работали корректно
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -U pip && pip install --no-cache-dir -r requirements.txt

COPY . .

# Телеграм-бот не слушает порт. Это worker-процесс.
ENTRYPOINT ["python","-u","bot.py"]
