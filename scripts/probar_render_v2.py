"""Prueba local del render v2 con material real, por el mismo camino que /render/v2.

Uso: GEO_DIR=<carpeta natural earth> python3 scripts/probar_render_v2.py \
        <narracion.wav|mp3> <guion.txt> <plan.json> <carpeta_ia> <salida.mp4> [--alignment eleven.json]
Sin --alignment usa Whisper (base, CPU) + correccion con el guion, como el servidor.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

from render_v2 import orquestador          # noqa: E402
from text_layer.alignment import words_from_whisper, align_script_to_words, words_from_payload  # noqa: E402


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                capture_output=True, text=True).stdout.strip())


def main():
    narr, guion, plan, carpeta_ia, salida = sys.argv[1:6]
    script = open(guion).read()
    d = dur(narr)
    t0 = time.time()
    if "--alignment" in sys.argv:
        words = words_from_payload(json.load(open(sys.argv[sys.argv.index("--alignment") + 1])))
    else:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        work0 = tempfile.mkdtemp()

        def cortar(inicio, duracion, _n=[0]):
            _n[0] += 1
            dst = os.path.join(work0, f"c{_n[0]:02d}.wav")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(inicio), "-t", str(duracion), "-i", narr,
                            "-ar", "16000", "-ac", "1", dst], check=True)
            return dst
        words, _ = words_from_whisper(model, narr, duration=d, cut=cortar, log=print)
    words = align_script_to_words(script, words)
    print(f"alineacion: {len(words)} palabras en {time.time() - t0:.0f}s")
    ia = {os.path.splitext(f)[0]: os.path.join(carpeta_ia, f) for f in os.listdir(carpeta_ia)}
    work = tempfile.mkdtemp(prefix="v2_")
    lib = os.path.join(AQUI, "music", "library")
    out, res = orquestador.run(script, words, json.load(open(plan)), narr, d, ia, work, lib,
                               semilla="prueba-local", build="local-v2",
                               riser=os.path.join(AQUI, "sfx", "riser_01_mixkit_1144.mp3"),
                               nproc=int(os.environ["NPROC"]) if os.environ.get("NPROC") else None)
    os.replace(out, salida)
    json.dump(res, open(salida + ".json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "creditos"}, ensure_ascii=False))
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
