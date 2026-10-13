"""Material de los planos: fotos reales de Wikimedia Commons (con candado de licencia),
mapas propios con Natural Earth (dominio publico) e imagenes de IA de EP-04 como reserva.

Candado de licencia de imagen: solo entran Public Domain, CC0, CC BY y CC BY-SA. Cada foto
guarda autor, licencia y pagina, que salen en pantalla y en `creditos` para la descripcion.
"""
import html
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("render_v2.activos")
UA = {"User-Agent": "EnigmasDelPasado-render/2.0 (canal documental; contacto via YouTube)"}
LICENCIA_OK = re.compile(r"^(public domain|pd\b|pd-|cc0|cc[ -]by(-sa)?[ -]?[1-4]\.0|cc[ -]by(-sa)?$|no restrictions)", re.I)
# Fotos que no queremos aunque la busqueda las devuelva: graficos, escudos, planos, logos.
TITULO_MALO = re.compile(r"(\.svg|logo|flag|bandera|coat of arms|escudo|diagram|chart|plano|map\b|mapa|"
                         r"icon|signature|firma|stamp|sello|banknote|coin|poster)", re.I)
# Fotos de fauna o flora (titulo con nombre cientifico entre parentesis): salen mucho al
# buscar reservas naturales y casi nunca son lo que el guion esta contando.
ESPECIE = re.compile(r"\([A-Z][a-z]+ [a-z]+\)")
GEO = os.environ.get("GEO_DIR", "/app/geo")


def _api(params, intentos=6):
    q = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    for i in range(intentos):
        try:
            with urllib.request.urlopen(urllib.request.Request(q, headers=UA), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                raise
            time.sleep(10 * (i + 1))
        except Exception:
            time.sleep(5 * (i + 1))
    return {}


def _limpio(m, k):
    return re.sub(r"<[^>]+>", "", html.unescape((m.get(k) or {}).get("value", ""))).strip()


class Commons:
    def __init__(self, carpeta):
        self.carpeta = carpeta
        os.makedirs(carpeta, exist_ok=True)
        self.cache = {}       # consulta -> [candidatos]
        self.usadas = set()   # titulos ya descargados
        self.fichas = {}      # clave -> ficha de licencia

    def buscar(self, consulta, n=12):
        if consulta in self.cache:
            return self.cache[consulta]
        r = _api({"action": "query", "format": "json", "generator": "search",
                  "gsrsearch": f"filetype:bitmap {consulta}", "gsrnamespace": 6, "gsrlimit": n,
                  "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920})
        out = []
        for p in (r.get("query", {}).get("pages", {}) or {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            m = ii.get("extmetadata", {})
            lic = _limpio(m, "LicenseShortName")
            w, h = ii.get("width", 0), ii.get("height", 0)
            if not LICENCIA_OK.match(lic) or TITULO_MALO.search(p["title"]) or ESPECIE.search(p["title"]):
                continue
            if w < 1400 or h < 700 or w / max(h, 1) < 1.0:
                continue          # pequenas o verticales: en 16:9 se verian mal
            out.append({"titulo": p["title"], "url": ii.get("thumburl") or ii.get("url"), "w": w, "h": h,
                        "licencia": lic, "autor": re.sub(r"\s+from\s.*$", "", _limpio(m, "Artist"))[:60],
                        "pagina": ii.get("descriptionurl"), "indice": p.get("index", 99)})
        out.sort(key=lambda x: x["indice"])
        self.cache[consulta] = out
        time.sleep(1.0)
        return out

    def siguiente(self, consultas):
        """Primera foto aun no usada de la lista de consultas. None si no hay ninguna."""
        for c in consultas:
            for cand in self.buscar(c):
                if cand["titulo"] in self.usadas:
                    continue
                clave = f"cm{len(self.fichas):03d}"
                destino = os.path.join(self.carpeta, clave + ".jpg")
                ok = False
                for intento in range(4):
                    try:
                        with urllib.request.urlopen(urllib.request.Request(cand["url"], headers=UA), timeout=120) as r:
                            data = r.read()
                        open(destino, "wb").write(data)
                        Image.open(destino).verify()
                        ok = True
                        break
                    except urllib.error.HTTPError as e:
                        if e.code != 429:
                            break
                        time.sleep(8 * (intento + 1))   # Wikimedia limita las descargas: esperar, no saltar
                    except Exception:
                        break
                if not ok:
                    log.warning("Commons: no se pudo bajar %s", cand["titulo"])
                    self.usadas.add(cand["titulo"])
                    continue
                self.usadas.add(cand["titulo"])
                self.fichas[clave] = dict(cand, archivo=destino, consulta=c)
                time.sleep(1.5)
                return clave
        return None


# ── mapas (Natural Earth, dominio publico) ─────────────────

_GEO_CACHE = {}


def _geo(nombre):
    if nombre not in _GEO_CACHE:
        ruta = os.path.join(GEO, nombre + ".geojson")
        _GEO_CACHE[nombre] = json.load(open(ruta))["features"] if os.path.exists(ruta) else []
    return _GEO_CACHE[nombre]


def _poligonos(geom, P):
    cs = geom["coordinates"]
    if geom["type"] == "Polygon":
        cs = [cs]
    for poly in cs:
        yield [P(*pt) for pt in poly[0]]


def mapa(destino, lat, lon, etiqueta, region=None, radio=3.2, W=5760, H=3240, fuente_titulo=None):
    """Mapa luminoso centrado en (lat, lon), con la region resaltada, carreteras y un punto."""
    k = math.cos(math.radians(lat))
    dlon = radio / max(k, 0.3)
    lon0, lon1 = lon - dlon * 16 / 9 / 1.0, lon + dlon * 16 / 9 / 1.0
    lat0, lat1 = lat - radio, lat + radio
    lon0, lon1 = lon - (lat1 - lat0) * 16 / 9 / k / 2, lon + (lat1 - lat0) * 16 / 9 / k / 2

    def P(x, y):
        return ((x - lon0) / (lon1 - lon0) * W, (lat1 - y) / (lat1 - lat0) * H)

    def visible(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return not (max(xs) < 0 or min(xs) > W or max(ys) < 0 or min(ys) > H)

    img = Image.new("RGB", (W, H), (22, 74, 96))
    land = Image.new("L", (W, H), 0)
    dl = ImageDraw.Draw(land)
    for f in _geo("ne_10m_admin_0_countries"):
        for ext in _poligonos(f["geometry"], P):
            if visible(ext):
                dl.polygon(ext, fill=255)
    for f in _geo("ne_10m_lakes"):
        for ext in _poligonos(f["geometry"], P):
            if visible(ext):
                dl.polygon(ext, fill=0)
    glow = land.filter(ImageFilter.GaussianBlur(W // 60))
    img = Image.composite(Image.new("RGB", (W, H), (54, 138, 160)), img, glow)
    grad = Image.new("RGB", (W, H))
    gd = ImageDraw.Draw(grad)
    for y in range(0, H, 2):
        t = y / H
        gd.rectangle([0, y, W, y + 2], fill=(int(226 - 76 * t), int(205 - 35 * t), int(150 - 58 * t)))
    if region:
        hl = Image.new("L", (W, H), 0)
        dh = ImageDraw.Draw(hl)
        for f in _geo("ne_10m_admin_1_states_provinces"):
            if (f["properties"].get("name") or "").lower() == region.lower():
                for ext in _poligonos(f["geometry"], P):
                    dh.polygon(ext, fill=255)
        grad = Image.composite(Image.blend(grad, Image.new("RGB", (W, H), (255, 214, 64)), 0.55), grad, hl)
    img = Image.composite(grad, img, land)
    d = ImageDraw.Draw(img)
    for f in _geo("ne_10m_admin_1_states_provinces"):
        for ext in _poligonos(f["geometry"], P):
            if visible(ext):
                d.line(ext + ext[:1], fill=(255, 255, 255), width=5)
    for f in _geo("ne_10m_roads"):
        g = f["geometry"]
        ls = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
        for l in ls:
            pts = [P(*p) for p in l]
            if visible(pts):
                d.line(pts, fill=(255, 186, 60), width=6)
    for f in _geo("ne_10m_admin_0_countries"):
        for ext in _poligonos(f["geometry"], P):
            if visible(ext):
                d.line(ext + ext[:1], fill=(70, 50, 30), width=10)
    x, y = P(lon, lat)
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(g).ellipse([x - 48, y - 48, x + 48, y + 48], fill=(235, 40, 30, 110))
    g = g.filter(ImageFilter.GaussianBlur(16))
    img.paste(g, (0, 0), g)
    d = ImageDraw.Draw(img)
    d.ellipse([x - 16, y - 16, x + 16, y + 16], fill=(235, 40, 30), outline=(255, 255, 255), width=6)
    if etiqueta and fuente_titulo:
        d.text((x + 40, y), etiqueta, font=ImageFont.truetype(fuente_titulo, 72), fill=(255, 255, 255),
               anchor="lm", stroke_width=6, stroke_fill=(15, 15, 15))
    img.save(destino, quality=92)
    return {"cx": x / W, "cy": y / H}


def geo_disponible():
    return all(os.path.exists(os.path.join(GEO, n + ".geojson"))
               for n in ("ne_10m_admin_0_countries", "ne_10m_admin_1_states_provinces"))
