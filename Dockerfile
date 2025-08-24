# ---- БАЗА ----
FROM python:3.11-slim

# 1) ffmpeg + мелочи
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 2) рабочая папка
WORKDIR /app

# 3) зависимости
COPY requirements.txt .
RUN pip install -U pip && pip install -r requirements.txt

# 4) код бота
COPY . .

# 5) запуск (stdout-логирование, без буфера)
CMD ["python", "-u", "bot.py"]
