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
COPY audio_mix.py .
COPY text_layer/ ./text_layer/
COPY music/ ./music/
COPY fonts/ ./fonts/
# Las fuentes de la capa de texto tambien en la ruta de fontconfig: libass las
# lee de /app/fonts via `fontsdir`, pero `fc-list` (que es como /health y
# /test-subs comprueban que no hay fallback) mira las rutas del sistema.
RUN mkdir -p /usr/share/fonts/truetype/ep \
    && cp /app/fonts/*.ttf /usr/share/fonts/truetype/ep/ \
    && fc-cache -f
COPY branding/ ./branding/
COPY sfx/ ./sfx/
RUN ls -la /app/music/library/ && test "$(ls /app/music/library/*.mp3 | wc -l)" -ge 8
RUN test -f /app/fonts/Anton-Regular.ttf
# Que las cuatro caras se resuelvan por nombre de familia. Una fuente que cae
# en sustitucion no da error en libass: sale un video con la tipografia
# equivocada y nadie se entera hasta verlo. Aqui rompe el build.
RUN for f in "Anton" "Cormorant Garamond Medium" "Cormorant Garamond Medium Italic" \
             "Cormorant Garamond SemiBold"; do \
      fc-list ":family=$f" file | grep -q . || { echo "FALTA la fuente: $f"; exit 1; }; \
    done \
    && ffmpeg -filters 2>/dev/null | grep -qw ass || { echo "ffmpeg sin libass"; exit 1; }
RUN test -f /app/branding/logo_ep.png
RUN test -f /app/sfx/riser_01_mixkit_1144.mp3
RUN python -c "import audio_mix; print('audio_mix OK')"
RUN python -c "import text_layer; print('text_layer OK')"

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "7200", "--workers", "1", "--log-level", "info", "render:app"]
