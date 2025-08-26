FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata ffmpeg file \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -U pip && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
