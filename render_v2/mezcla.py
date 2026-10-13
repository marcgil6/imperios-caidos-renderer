"""Audio del render v2: voz a -14,5 LUFS, cama de dos pistas certificadas a -30 LUFS con
ducking, efectos sintetizados y limitador a -1 dBTP. Mismo criterio que el piloto."""
import logging
import os
import subprocess

import numpy as np

import sello_musica
from . import sfx as S

log = logging.getLogger("render_v2.mezcla")
# Plan por defecto (fracciones del guion): intriga -> investigacion -> revelacion -> cierre.
PLAN_DEFECTO = [{"start": 0.0, "end": 0.28, "mood": "mystery", "intensity": "medium"},
                {"start": 0.28, "end": 0.55, "mood": "ancient", "intensity": "medium"},
                {"start": 0.55, "end": 0.82, "mood": "discovery", "intensity": "medium"},
                {"start": 0.82, "end": 1.0, "mood": "emotional", "intensity": "low"}]


def cama(work, library_dir, legacy, dur, plan=None, avoid=(), semilla=None, lufs_cache=None):
    """Cama musical con el motor de produccion (audio_mix): solo pistas certificadas."""
    import audio_mix
    library = audio_mix.load_library(library_dir, legacy)
    segs = audio_mix.normalize_plan(plan or PLAN_DEFECTO, dur, 0.0)
    chosen = audio_mix.select_tracks(library, segs, avoid=list(avoid or []), seed=semilla)
    bed = audio_mix.build_bed(work, chosen, dur, lufs_cache)
    # Mismo formato que /render (EP-07 escribe musica_usada con id, mood y tramo)
    return bed, [{"id": c["track"]["id"], "title": c["track"].get("title"), "mood": c["mood"],
                  "intensity": c["intensity"], "start": round(c["start"], 1), "end": round(c["end"], 1)}
                 for c in chosen]


def _leer(path):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(S.SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def pista_efectos(tl, destino, riser_path=None):
    n = int(tl["dur"] * S.SR) + S.SR
    x = np.zeros(n, np.float32)

    def poner(y, t, g):
        a = int(max(0, t) * S.SR)
        b = min(n, a + len(y))
        if b > a:
            x[a:b] += y[:b - a] * g
    cache = {}
    for e in tl["sfx"]:
        k = e["k"]
        if k == "ambiente":
            tipo = e.get("tipo", "aire")
            if tipo == "selva":
                poner(S.ambiente_selva(e["dur"]), 0, 0.10)
            elif tipo != "ninguno":
                poner(S.ambiente_aire(e["dur"]), 0, 0.07)
        elif k == "whoosh":
            poner(cache.setdefault(k, S.whoosh(0.55)), e["t"], 0.55)
        elif k == "whoosh_suave":
            poner(cache.setdefault(k, S.whoosh(0.8)), e["t"], 0.28)
        elif k == "impacto":
            poner(cache.setdefault(k, S.impacto()), e["t"], 0.65)
        elif k == "impacto_suave":
            poner(cache.setdefault(k, S.impacto(1.2, 62)), e["t"], 0.38)
        elif k == "riser" and riser_path and os.path.exists(riser_path):
            r = cache.get("riser")
            if r is None:
                r = _leer(riser_path)[:int(2.6 * S.SR)]
                r = r / (np.abs(r).max() + 1e-9) * 0.5
                cache["riser"] = r
            poner(r, e["t"], 0.5)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(S.SR), "-ac", "1", "-i", "-", destino],
                   input=x.tobytes(), check=True)


def mezclar(narracion, efectos, bed, dur, destino):
    """Voz a -14,5 LUFS, cama ya a -30 LUFS con ducking por la voz, efectos y limitador."""
    if bed:
        fc = ("[0:a]loudnorm=I=-13.7:TP=-1.5:LRA=7,aresample=48000,asplit[voz][vozsc];"
              "[1:a]aresample=48000[cama];"
              "[cama][vozsc]sidechaincompress=threshold=0.1:ratio=3:attack=20:release=1500[camad];"
              "[2:a]aresample=48000,volume=0.9[ef];"
              "[voz][camad][ef]amix=inputs=3:normalize=0,alimiter=limit=0.89:level=false[mix]")
        entradas = ["-i", narracion, "-i", bed, "-i", efectos]
    else:
        fc = ("[0:a]loudnorm=I=-13.7:TP=-1.5:LRA=7,aresample=48000[voz];[1:a]aresample=48000,volume=0.9[ef];"
              "[voz][ef]amix=inputs=2:normalize=0,alimiter=limit=0.89:level=false[mix]")
        entradas = ["-i", narracion, "-i", efectos]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *entradas, "-filter_complex", fc, "-map", "[mix]",
                    "-t", f"{dur:.3f}", "-c:a", "pcm_s16le", destino], check=True)
