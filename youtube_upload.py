"""Subida programada a YouTube.

El MP4 de un vídeo pesa 1,2-1,4 GB. n8n mete los binarios en memoria, así que la
subida no puede pasar por allí: la hace este servicio, que ya baja de Drive y
tiene disco.

La clave del diseño es que aquí NO hay ningún cron de publicación. Se sube el
vídeo en privado con `status.publishAt` y es YouTube quien lo publica a la hora
exacta, aunque no haya nada encendido. Eso es lo que permite generar con una
semana de antelación y revisar sin prisa.

Credenciales: OAuth de usuario, no cuenta de servicio — una cuenta de servicio no
puede ser dueña de un canal de YouTube. Van por entorno (YT_CLIENT_ID,
YT_CLIENT_SECRET, YT_REFRESH_TOKEN) y salen del cliente de escritorio
"EP uploader mac", que está en un proyecto de Google Cloud publicado: su refresh
token no caduca a los 7 días como el de los proyectos en modo "Prueba".
"""

import logging
import os

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger("render")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

# Cada videos.insert cuesta 1.600 unidades de una cuota diaria de 10.000, así que
# el techo real son 6 subidas al día. thumbnails.set son 50 más.
QUOTA_INSERT = 1600
QUOTA_THUMBNAIL = 50
QUOTA_UPDATE = 50


class YouTubeAuthError(RuntimeError):
    pass


# Forma esperada de cada credencial. No basta con que la variable exista: si
# /health dice que si con un valor basura, da luz verde para una subida que
# muere en la autenticacion DESPUES de bajar 1,4 GB de Drive. Paso de verdad:
# las tres llegaron con el mismo valor de 12 caracteres y /health lo dio por
# bueno.
_FORMA = {
    "YT_CLIENT_ID": (lambda v: v.endswith(".apps.googleusercontent.com"),
                     "debe acabar en .apps.googleusercontent.com"),
    "YT_CLIENT_SECRET": (lambda v: v.startswith("GOCSPX-") and len(v) > 20,
                         "debe empezar por GOCSPX-"),
    "YT_REFRESH_TOKEN": (lambda v: v.startswith("1//") and len(v) > 40,
                         "debe empezar por 1//"),
}


def credentials_problems():
    """Lista de problemas con las credenciales. Vacia = tienen buena pinta."""
    fallos = []
    for nombre, (valida, esperado) in _FORMA.items():
        v = (os.environ.get(nombre) or "").strip()
        if not v:
            fallos.append(f"{nombre}: sin definir")
        elif not valida(v):
            fallos.append(f"{nombre}: {esperado} (tiene {len(v)} caracteres)")
    return fallos


def credentials_configured():
    return not credentials_problems()


def _service():
    if not credentials_configured():
        raise YouTubeAuthError(
            "Faltan YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN en el entorno"
        )
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(GoogleRequest())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _normalize_tags(tags):
    """Acepta lista o la cadena separada por comas que guarda Airtable."""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    out = []
    for t in tags:
        t = str(t).strip().lstrip("#")
        if t and t not in out:
            out.append(t)
    # YouTube corta a 500 caracteres contando las comas; recortamos nosotros para
    # que el recorte sea por etiqueta entera y no a media palabra.
    kept, total = [], 0
    for t in out:
        total += len(t) + 1
        if total > 480:
            break
        kept.append(t)
    return kept


def upload(video_path, *, titulo, descripcion, tags=None, publish_at=None,
           categoria="24", idioma="es", made_for_kids=False,
           thumbnail_path=None):
    """Sube un MP4 en privado, programado si se da publish_at.

    publish_at: ISO 8601 en UTC con 'Z' (p. ej. "2026-10-01T16:00:00Z").
    Devuelve {video_id, url, publish_at, privacy, thumbnail_set, quota}.
    """
    yt = _service()

    status = {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": bool(made_for_kids),
        "embeddable": True,
    }
    if publish_at:
        # Con publishAt, YouTube exige que el vídeo esté en privado. Cuando llega
        # la hora lo pasa a público él solo.
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": titulo[:100],
            "description": descripcion[:5000],
            "tags": _normalize_tags(tags),
            "categoryId": str(categoria),
            "defaultLanguage": idioma,
            "defaultAudioLanguage": idioma,
        },
        "status": status,
    }

    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    log.info("YouTube — subiendo %.0f MB, publishAt=%s", size_mb, publish_at or "(sin programar)")

    media = MediaFileUpload(
        video_path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True
    )
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response, last_logged = None, -1
    while response is None:
        chunk_status, response = req.next_chunk()
        if chunk_status:
            pct = int(chunk_status.progress() * 100)
            # Un log por cada 10% — con 1,4 GB en trozos de 8 MB son ~175 llamadas.
            if pct // 10 > last_logged:
                last_logged = pct // 10
                log.info("YouTube — %d%%", pct)

    video_id = response["id"]
    quota = QUOTA_INSERT
    log.info("YouTube — subido: %s", video_id)

    thumbnail_set = False
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            yt.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path),
            ).execute()
            thumbnail_set = True
            quota += QUOTA_THUMBNAIL
        except HttpError as e:
            # Una miniatura que falla no justifica perder una subida de 1.600
            # unidades: se puede poner después a mano o por EP-09.
            log.error("YouTube — thumbnails.set falló: %s", e)

    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "publish_at": publish_at,
        "privacy": "private",
        "thumbnail_set": thumbnail_set,
        "quota": quota,
    }


def unschedule(video_id):
    """Quita la fecha de publicación: el vídeo se queda en privado indefinidamente.

    Es lo que hace el botón PARAR de Telegram. videos.update con part=status
    REEMPLAZA el objeto status entero, así que hay que leerlo antes y reenviarlo
    sin publishAt — si no, se pierden made-for-kids y embeddable.
    """
    yt = _service()
    current = yt.videos().list(part="status", id=video_id).execute()
    items = current.get("items", [])
    if not items:
        raise ValueError(f"El vídeo {video_id} no existe o no es de este canal")

    status = items[0]["status"]
    status.pop("publishAt", None)
    status["privacyStatus"] = "private"
    # Solo se quitan los campos de solo lectura. license y publicStatsViewable SÍ
    # son escribibles y hay que reenviarlos o se pierden.
    for ro in ("uploadStatus", "failureReason", "rejectionReason"):
        status.pop(ro, None)

    yt.videos().update(part="status", body={"id": video_id, "status": status}).execute()
    log.info("YouTube — %s desprogramado, queda en privado", video_id)
    return {"video_id": video_id, "privacy": "private", "publish_at": None,
            "quota": QUOTA_UPDATE + 1}
