# ---- Runtime image ----------------------------------------------------------
FROM python:3.11-slim

# Системные пакеты: tzdata + ffmpeg (для конвертации голосовых)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — для кэширования слоёв
COPY requirements.txt .
RUN python -m pip install -U pip && \
    pip install --no-cache-dir -r requirements.txt

# Потом исходники
COPY . .

# Запуск
CMD ["python", "-u", "bot.py"]
