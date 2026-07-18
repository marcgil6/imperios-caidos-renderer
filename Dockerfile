FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fontconfig libgomp1 \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Chromium para el modo HTML del endpoint /thumbnail (miniaturas EP-08)
RUN playwright install --with-deps chromium

COPY render.py .
COPY music/ ./music/
COPY fonts/ ./fonts/
COPY branding/ ./branding/
COPY sfx/ ./sfx/
RUN ls -la /app/music/ && test -f /app/music/music_01_uprising.mp3
RUN test -f /app/fonts/Anton-Regular.ttf
RUN test -f /app/branding/logo_ep.png
RUN test -f /app/sfx/riser_01_mixkit_1144.mp3

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "7200", "--workers", "1", "--log-level", "info", "render:app"]
