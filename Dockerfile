FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fontconfig libgomp1 \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render.py .
COPY music/ ./music/
COPY fonts/ ./fonts/
RUN ls -la /app/music/ && test -f /app/music/music_01_uprising.mp3
RUN test -f /app/fonts/Anton-Regular.ttf

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "7200", "--workers", "1", "--log-level", "info", "render:app"]
