"""Orquesta un render v2 completo a partir de material ya descargado.

render.py se encarga de Drive (bajar la voz y las imagenes de IA, subir el MP4); aqui
solo hay planificacion, dibujo en paralelo, mezcla y sello, para poder probarlo en local.
"""
import json
import logging
import multiprocessing as mp
import os
import subprocess

from . import motor, planificador, mezcla, activos

log = logging.getLogger("render_v2")

_TL = None


def _tramo(args):
    k, f0, f1, ruta_tl, rutas, salida = args
    motor.RUTAS.clear()
    motor.RUTAS.update(rutas)
    tl = json.load(open(ruta_tl))
    pr = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{motor.W}x{motor.H}", "-r", str(motor.FPS), "-i", "-",
                           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p", "-g", "60", salida],
                          stdin=subprocess.PIPE)
    for f in range(f0, f1):
        pr.stdin.write(motor.fotograma(tl, f / motor.FPS).tobytes())
    pr.stdin.close()
    if pr.wait() != 0:
        raise RuntimeError(f"ffmpeg fallo en el tramo {k}")
    return salida


def video(tl, rutas, work, nproc=None):
    ruta_tl = os.path.join(work, "timeline.json")
    json.dump(tl, open(ruta_tl, "w"), ensure_ascii=False)
    nproc = nproc or max(1, min(12, (os.cpu_count() or 2) - 1))
    nf = int(tl["dur"] * motor.FPS)
    tramos = max(nproc, min(48, nf // 900))      # tramos de ~30 s: reparto parejo aunque haya planos caros
    cortes = [round(nf * i / tramos) for i in range(tramos + 1)]
    args = [(k, cortes[k], cortes[k + 1], ruta_tl, rutas, os.path.join(work, f"seg_{k:03d}.mp4")) for k in range(tramos)]
    ctx = mp.get_context("fork")
    with ctx.Pool(nproc) as pool:
        segs = pool.map(_tramo, args, chunksize=1)
    lista = os.path.join(work, "segs.txt")
    open(lista, "w").write("\n".join(f"file '{s}'" for s in segs))
    salida = os.path.join(work, "video_v2.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lista, "-c", "copy", salida], check=True)
    return salida


def run(script_text, palabras, plan, narracion, dur, ia, work, library_dir, semilla, build,
        music_avoid=(), music_plan=None, legacy=None, riser=None, nproc=None):
    """Devuelve (ruta_mp4, informe)."""
    commons = activos.Commons(os.path.join(work, "commons"))
    os.makedirs(os.path.join(work, "mapas"), exist_ok=True)
    tl, creditos, rutas = planificador.construir(script_text, palabras, plan, dur, commons, ia,
                                                 os.path.join(work, "mapas"), semilla,
                                                 fuente_titulo=motor.FUENTES["anton"])
    if not tl["planos"]:
        raise RuntimeError("El plan visual no produjo ningun plano.")
    res = planificador.resumen(tl)
    log.info("Plan v2: %s", res)
    motor.RUTAS.clear()
    motor.RUTAS.update(rutas)
    v = video(tl, rutas, work, nproc)
    ef = os.path.join(work, "efectos.wav")
    mezcla.pista_efectos(tl, ef, riser)
    try:
        bed, usadas = mezcla.cama(work, library_dir, legacy, tl["dur"], music_plan, music_avoid, semilla)
    except Exception as e:
        log.warning("Cama musical fallo (%s): voz y efectos sin musica", e)
        bed, usadas = None, []
    audio = os.path.join(work, "mezcla.wav")
    mezcla.mezclar(narracion, ef, bed, tl["dur"], audio)
    import sello_musica
    estado = sello_musica.MUSICA_LIBRE if usadas else sello_musica.SIN_MUSICA
    salida = os.path.join(work, "final_v2.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", v, "-i", audio, "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                    *sello_musica.args_sello(estado, [u["id"] for u in usadas], build), salida], check=True)
    res.update(creditos=creditos, music_tracks=usadas, commons_fotos=len(commons.fichas))
    return salida, res
