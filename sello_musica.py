"""Candado de licencia de la música: qué pistas pueden sonar y qué MP4 se pueden subir.

Por qué existe (25/09/2026): el vídeo 4 se subió con un MP4 renderizado el 05/08,
cuando la cama eran pistas de Scott Buckley (CC BY, con Smart Content ID), y YouTube
lo reclamó. La biblioteca ya estaba limpia desde el 31/08; lo que falló fue que nada
impedía subir un fichero viejo. Dos candados:

1. Entrada: solo suena una pista que esté en `library.json` con origen Biblioteca de
   audio de YouTube, sin atribución y con el sha256 que coincide. Un mp3 copiado a la
   carpeta sin pasar por el manifiesto se ignora.
2. Salida: cada MP4 que produce el servicio lleva un sello en la etiqueta `comment`
   (`ep-music=...`). `youtube_upload.upload` y `make_short.py` rechazan cualquier
   fichero sin sello válido. Un MP4 renderizado antes de esto no tiene sello, así que
   no se puede subir por error.

Sin dependencias de Google ni de Flask: lo importan render.py, youtube_upload.py y
los scripts locales.
"""

import hashlib
import json
import os
import subprocess

CLAVE = "ep-music"
# Estados válidos del sello. "none" = narración sola (fallaron los dos motores de
# mezcla): no lleva música, así que tampoco hay nada que reclamar.
MUSICA_LIBRE = "yt-audio-library"
SIN_MUSICA = "none"
ESTADOS_VALIDOS = (MUSICA_LIBRE, SIN_MUSICA)

ORIGEN_OK = "YouTube Audio Library"


class MusicaNoCertificada(RuntimeError):
    pass


# ── Candado de entrada: pistas autorizadas ─────────────────

_hash_cache = {}


def _sha256(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    if key not in _hash_cache:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for bloque in iter(lambda: fh.read(1 << 20), b""):
                h.update(bloque)
        _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def cargar_manifiesto(library_dir):
    """{nombre de fichero: entrada} de library.json, o {} si no hay manifiesto."""
    ruta = os.path.join(library_dir, "library.json")
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {t["file"]: t for t in data.get("tracks", []) if t.get("file")}


def motivo_rechazo(path, manifiesto):
    """None si la pista puede sonar; si no, el motivo en una frase."""
    fn = os.path.basename(path)
    t = manifiesto.get(fn)
    if not t:
        return "no está en library.json"
    if t.get("source") != ORIGEN_OK:
        return f"origen {t.get('source')!r}, se exige {ORIGEN_OK!r}"
    if t.get("attribution"):
        return "exige atribución (licencia con Content ID)"
    if not t.get("sha256"):
        return "library.json no guarda su sha256"
    if not os.path.isfile(path):
        return "el fichero no existe"
    if _sha256(path) != t["sha256"]:
        return "el fichero no coincide con el sha256 del manifiesto"
    return None


def pista_autorizada(path, library_dir=None):
    library_dir = library_dir or os.path.dirname(path)
    return motivo_rechazo(path, cargar_manifiesto(library_dir)) is None


# ── Candado de salida: sello en el MP4 ─────────────────────

def texto_sello(estado, pistas=(), build=None):
    if estado not in ESTADOS_VALIDOS:
        raise ValueError(f"estado de sello desconocido: {estado}")
    partes = [f"{CLAVE}={estado}"]
    if pistas:
        partes.append("tracks=" + ",".join(pistas))
    if build:
        partes.append(f"build={build}")
    return ";".join(partes)


def args_sello(estado, pistas=(), build=None):
    """Argumentos de ffmpeg que escriben el sello (van antes del fichero de salida)."""
    return ["-metadata", "comment=" + texto_sello(estado, pistas, build)]


def leer_sello(path):
    """Diccionario del sello del MP4, o None si no lo lleva."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60)
    comentario = (r.stdout or "").strip()
    if not comentario.startswith(CLAVE + "="):
        return None
    sello = {}
    for parte in comentario.split(";"):
        k, _, v = parte.partition("=")
        sello[k.strip()] = v.strip()
    return sello


def exigir_sello(path):
    """Lanza MusicaNoCertificada si el MP4 no salió del motor con música libre."""
    if not os.path.isfile(path):
        raise MusicaNoCertificada(f"no existe {path}")
    sello = leer_sello(path)
    if not sello:
        raise MusicaNoCertificada(
            f"{os.path.basename(path)} no lleva sello de música ({CLAVE}). Es un "
            "render anterior al candado de licencia o no salió del servicio: puede "
            "llevar música con Content ID. Vuelve a mezclarlo con /remix.")
    if sello.get(CLAVE) not in ESTADOS_VALIDOS:
        raise MusicaNoCertificada(
            f"{os.path.basename(path)}: sello de música desconocido {sello.get(CLAVE)!r}")
    return sello
