"""Del plan visual de Claude + los tiempos reales de la voz a una linea de tiempo de planos.

Estilo del piloto v3 (aprobado por Marc el 13/10/2026): la imagen cambia CUANDO SE NOMBRA lo que
muestra. Cada plano del plan trae "desde" (la palabra exacta en la que entra) y las superposiciones
traen "frase" (la palabra en la que aparecen). Reglas de ritmo, medidas sobre la v3:
  * corte minimo 1,8 s (si dos anclas caen mas cerca, gana la primera),
  * un tramo de mas de 5,5 s se parte con otro encuadre de la misma imagen (~3,7 s de media),
  * sin "desde", el material del parrafo se reparte a ~3,7 s.
"""
import hashlib
import logging
import math
import os
import re
import unicodedata

from . import activos

log = logging.getLogger("render_v2.plan")
SEG_POR_PLANO = 4.0     # objetivo de las segundas tomas; media resultante ~3,7 s (v3)
MIN_PLANO = 1.8         # minimo de las segundas tomas automaticas
MIN_ANCLA = 0.9         # minimo entre cortes anclados a palabra (en la v3 habia golpes de 1,0-1,6 s)
MAX_PLANO = 6.0
BREAK = re.compile(r"<break[^>]*/?>")
NO_REPETIR = ("titulo", "cita", "mapa", "escaneo")


def _norm(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def parrafos_de(script_text):
    pars = [BREAK.sub(" ", p).strip() for p in re.split(r"\n\s*\n", script_text or "")]
    return [p for p in pars if p]


def _rnd(semilla, i, n):
    return int(hashlib.md5(f"{semilla}:{i}".encode()).hexdigest()[:8], 16) % max(n, 1)


def movimiento(semilla, i):
    """Movimiento visible (10-16 %), variado y repetible para un mismo video."""
    k = _rnd(semilla, i, 6)
    a = 0.10 + 0.06 * _rnd(semilla, i + 999, 100) / 100
    return [{"cx": [.5, .5], "cy": [.5, .5], "z": [1.0, 1.0 - a]},
            {"cx": [.5, .5], "cy": [.5, .5], "z": [1.0 - a, 1.0]},
            {"cx": [.5 - a / 1.5, .5 + a / 1.5], "cy": [.5, .5], "z": [.8, .8]},
            {"cx": [.5 + a / 1.5, .5 - a / 1.5], "cy": [.5, .5], "z": [.8, .8]},
            {"cx": [.45, .55], "cy": [.55, .45], "z": [.95, .95 - a]},
            {"cx": [.55, .45], "cy": [.45, .55], "z": [.85, .85 + a]}][k]


def segunda_toma(semilla, i):
    """Otro encuadre de la misma imagen (detalle) para partir un tramo largo, como en la v3."""
    cx = [.35, .65, .5, .4, .6][_rnd(semilla, i, 5)]
    cy = [.4, .55, .45][_rnd(semilla, i + 7, 3)]
    return {"cx": [cx, cx + (.05 if cx < .5 else -.05)], "cy": [cy, cy], "z": [.62, .52]}


def _foco(ruta):
    try:
        import thumbnail_v3
        from PIL import Image
        return thumbnail_v3.foco_auto(thumbnail_v3._cubrir(Image.open(ruta)))
    except Exception:
        return (0.6, 0.45)


def construir(script_text, palabras, plan, dur_total, commons, ia, carpeta_mapas, semilla,
              fuente_titulo=None):
    """palabras: Word(word, start, end) alineadas con los tokens del guion. ia: {nombre: ruta}.
    Devuelve (timeline, creditos, rutas)."""
    pars = parrafos_de(script_text)
    toks_par = [p.split() for p in pars]
    n_tok = sum(len(t) for t in toks_par)
    rutas = dict(ia)

    def tiempo(idx):
        if not palabras:
            return dur_total * idx / max(n_tok, 1)
        if len(palabras) != n_tok:
            idx = int(idx * len(palabras) / max(n_tok, 1))
        return palabras[min(idx, len(palabras) - 1)].start

    plan_pars = {int(p.get("p", i)): p for i, p in enumerate(plan.get("parrafos") or [])}
    lugar = plan.get("lugar_principal") or {}
    planos, subs, sfx = [], [], []
    ia_libres = sorted(ia)
    ia_usadas = set()
    mapas_hechos = {}
    focos = {}

    def ia_cualquiera(preferida=None):
        if preferida and preferida in ia:
            ia_usadas.add(preferida)
            return preferida
        libres = [k for k in ia_libres if k not in ia_usadas] or ia_libres
        if not libres:
            vistos = [pl["src"] for pl in planos if pl.get("tipo") == "img" and pl.get("src")]
            return vistos[_rnd(semilla, len(planos), len(vistos))] if vistos else None
        k = libres[_rnd(semilla, len(planos), len(libres))]
        ia_usadas.add(k)
        return k

    def credito_de(clave, pie=""):
        if clave in commons.fichas:
            f = commons.fichas[clave]
            return ((pie + " · ") if pie else "") + f"Foto: {f['autor'] or 'Wikimedia Commons'} · {f['licencia']}"
        if clave and clave.startswith("mapa"):
            return "Mapa: Natural Earth (dominio público) · elaboración propia"
        return "Recreación artística (IA)"

    def resolver_imagen(m):
        """Clave de imagen para un material foto/ia (con reserva). None si no hay nada."""
        tipo = (m.get("tipo") or "ia").lower()
        if tipo in ("foto", "escaneo") and m.get("buscar"):
            consultas = m["buscar"] if isinstance(m["buscar"], list) else [m["buscar"]]
            clave = commons.siguiente(consultas)
            if clave:
                rutas[clave] = commons.fichas[clave]["archivo"]
                return clave
            return ia_cualquiera(m.get("ia"))
        return ia_cualquiera(m.get("img") or m.get("ia"))

    base = 0
    for pi, toks in enumerate(toks_par):
        t_ini = tiempo(base) if pi else 0.0
        t_fin = tiempo(base + len(toks)) if pi + 1 < len(toks_par) else dur_total
        starts = [tiempo(base + k) for k in range(len(toks))]
        norm_toks = [_norm(t) for t in toks]
        pp = plan_pars.get(pi, {})
        material = [m for m in (pp.get("planos") or []) if isinstance(m, dict)] or [{"tipo": "ia"}]

        # 1) anclas: cada material entra en su palabra (busqueda en orden); el primero, al inicio
        anclas, j0 = [], 0
        for k, m in enumerate(material):
            t = None
            if k == 0:
                t = t_ini
            elif m.get("desde"):
                obj = _norm(str(m["desde"]).split()[0])
                for j in range(j0, len(toks)):
                    if obj and norm_toks[j].startswith(obj):
                        t, j0 = starts[j], j + 1
                        break
            anclas.append([t, m])
        sin = [a for a in anclas if a[0] is None]       # sin palabra: se reparten en el hueco mayor
        if sin:
            con = sorted(a[0] for a in anclas if a[0] is not None) + [t_fin]
            hueco = max(zip(con, con[1:]), key=lambda x: x[1] - x[0])
            for q, a in enumerate(sin):
                a[0] = hueco[0] + (hueco[1] - hueco[0]) * (q + 1) / (len(sin) + 1)
        anclas.sort(key=lambda a: a[0])
        cortes = []
        for t, m in anclas:
            if cortes and (t - cortes[-1][0] < MIN_ANCLA or t_fin - t < 1.0):
                continue
            cortes.append((t, m))

        # 2) resolver cada corte y partir los tramos largos con una segunda toma
        primer = len(planos)
        usados = set()
        for k, (c0, m) in enumerate(cortes):
            c1 = cortes[k + 1][0] if k + 1 < len(cortes) else t_fin
            tipo = (m.get("tipo") or "ia").lower()
            if tipo in NO_REPETIR and id(m) in usados:
                tipo, m = "ia", {"tipo": "ia"}
            usados.add(id(m))
            pl = {"t0": round(c0, 3), "par": pi}
            if tipo == "mapa":
                tipo = "ia"
                if activos.geo_disponible():
                    try:
                        lat, lon = float(m["lat"]), float(m["lon"])
                        key = (round(lat, 2), round(lon, 2), m.get("etiqueta", ""), m.get("radio", 3.2))
                        if key not in mapas_hechos:
                            clave = f"mapa{len(mapas_hechos):02d}"
                            destino = os.path.join(carpeta_mapas, clave + ".jpg")
                            punto = activos.mapa(destino, lat, lon, (m.get("etiqueta") or "").upper(), region=m.get("region"),
                                                 radio=float(m.get("radio", 3.2)), fuente_titulo=fuente_titulo)
                            mapas_hechos[key] = (clave, punto)
                            rutas[clave] = destino
                        clave, punto = mapas_hechos[key]
                        pl.update(tipo="img", src=clave, credito=credito_de(clave),
                                  mov={"cx": [.5, punto["cx"]], "cy": [.5, punto["cy"]], "z": [.75, .3]})
                        tipo = "mapa"
                    except Exception as e:
                        log.warning("Mapa no generado (%s): uso imagen", e)
            if tipo in ("titulo", "cita"):
                fondo = next((x.get("src") or x.get("b") for x in reversed(planos) if x.get("tipo") in ("img", "scan")), None)
                if fondo:
                    pl.update(tipo=tipo, fondo=fondo, texto=str(m.get("texto", ""))[:120], sub=m.get("sub", ""),
                              autor=m.get("autor", ""), flash=(tipo == "titulo"), credito=credito_de(fondo))
                    if tipo == "titulo":
                        sfx += [{"t": c0 - 2.6, "k": "riser"}, {"t": c0, "k": "impacto"}]
                    else:
                        pl["trans"], pl["tdur"] = "fundido", 0.45
                else:
                    tipo = "ia"
            if tipo == "escaneo":
                b = resolver_imagen(m)
                # parte de la ultima imagen mostrada que sea DISTINTA de la que se revela
                a = next((x.get("src") or x.get("b") for x in reversed(planos)
                          if x.get("tipo") in ("img", "scan") and (x.get("src") or x.get("b")) != b), None)
                if a and b and a != b:
                    pl.update(tipo="scan", a=a, b=b, mov=movimiento(semilla, len(planos)), credito=credito_de(b, m.get("pie", "")))
                    sfx.append({"t": c0 + 0.2, "k": "escaneo"})
                elif b:
                    pl.update(tipo="img", src=b, mov=movimiento(semilla, len(planos)), credito=credito_de(b, m.get("pie", "")))
            if "tipo" not in pl:
                clave = resolver_imagen(m)
                if not clave:
                    continue
                pl.update(tipo="img", src=clave, mov=movimiento(semilla, len(planos)), credito=credito_de(clave, m.get("pie", "")))
            planos.append(pl)
            dur = c1 - c0
            if dur > MAX_PLANO and pl["tipo"] in ("img", "scan"):
                n = math.ceil(dur / SEG_POR_PLANO)
                for q in range(1, n):
                    objetivo = c0 + dur * q / n
                    t2 = min((s for s in starts if c0 + MIN_PLANO <= s <= c1 - MIN_PLANO), key=lambda s: abs(s - objetivo), default=None)
                    if t2 is None or t2 - planos[-1]["t0"] < MIN_PLANO:
                        continue
                    src = pl.get("src") or pl.get("b")
                    seguidas = 0
                    for x in reversed(planos):
                        if (x.get("src") or x.get("b")) != src:
                            break
                        seguidas += 1
                    if seguidas >= 2:
                        # Como mucho dos tomas seguidas de la misma imagen: a partir de ahi, otra imagen
                        otra = ia_cualquiera()
                        if otra and otra != src:
                            planos.append({"t0": round(t2, 3), "par": pi, "tipo": "img", "src": otra, "auto": True,
                                           "mov": movimiento(semilla, len(planos)), "credito": credito_de(otra)})
                            continue
                    planos.append({"t0": round(t2, 3), "par": pi, "tipo": "img", "src": src, "auto": True,
                                   "mov": segunda_toma(semilla, len(planos)), "credito": pl["credito"]})

        # 3) superposiciones en su palabra
        for ov in (pp.get("superpuestos") or []):
            k = (ov.get("tipo") or "").lower()
            t_ov = t_ini + 0.3
            if ov.get("frase"):
                obj = _norm(str(ov["frase"]).split()[0])
                for j, nt in enumerate(norm_toks):
                    if obj and nt.startswith(obj):
                        t_ov = starts[j]
                        break
            if len(planos) <= primer:
                continue
            i_dest = max((i for i in range(primer, len(planos)) if planos[i]["t0"] <= t_ov + 0.01), default=primer)
            pl = planos[i_dest]
            d = max(0.0, t_ov - pl["t0"])
            if k == "placa":
                o = {"k": "placa", "l1": str(ov.get("l1", "")).upper()[:40], "l2": str(ov.get("l2", ""))[:50], "d": d, "dur": 4.2}
            elif k == "cifra":
                g = str(ov.get("grande", ""))[:14]
                o = {"k": "cifra", "grande": g, "peq": str(ov.get("peq", "")).upper()[:40], "d": d, "dur": 3.4,
                     # cuenta hacia arriba solo cantidades (6.764, 450), nunca anos (2013, 849)
                     "contar": bool(g.split() and re.fullmatch(r"\d[\d.]{2,}", g.split()[0])
                                    and not re.fullmatch(r"(1\d|20)\d\d|\d{3}", g.split()[0]))}
                sfx.append({"t": t_ov, "k": "impacto_suave"})
            elif k == "nombre":
                o = {"k": "nombre", "n": str(ov.get("n", "")).upper()[:32], "r": str(ov.get("r", ""))[:60], "d": d, "dur": 3.5}
            elif k == "marca" and pl.get("tipo") == "img":
                src = pl["src"]
                if src not in focos:
                    focos[src] = _foco(rutas.get(src, ""))
                o = {"k": "marca", "txt": str(ov.get("texto", "")).upper()[:22], "x": focos[src][0], "y": focos[src][1],
                     "d": d, "dur": 3.0, "r": 60}
                sfx.append({"t": t_ov + 0.15, "k": "pulso"})
            elif k == "destello":
                pl["flash_en"] = round(t_ov, 3)
                sfx.append({"t": t_ov, "k": "impacto"})
                continue
            elif k == "capitulo":
                pl0 = planos[primer]
                pl0["trans"], pl0["tdur"] = "barrido", 0.25
                pl0.setdefault("ovs", []).append({"k": "capitulo", "num": str(ov.get("num", "")), "txt": str(ov.get("txt", "")).upper()[:28], "d": 0, "dur": 1.6})
                sfx.append({"t": pl0["t0"], "k": "impacto_suave"})
                continue
            else:
                continue
            pl.setdefault("ovs", []).append(o)
        # una toma automatica no puede cortar un rotulo que se esta mostrando
        i = primer
        while i < len(planos):
            for o in planos[i].get("ovs", []):
                fin_ov = planos[i]["t0"] + o.get("d", 0) + o["dur"]
                planos[i + 1:] = [x for x in planos[i + 1:] if not (x.get("auto") and x["t0"] < fin_ov)]
            # si el plano protegido queda largo, segunda toma justo cuando acaba el rotulo
            t1 = planos[i + 1]["t0"] if i + 1 < len(planos) else t_fin
            if planos[i]["tipo"] == "img" and t1 - planos[i]["t0"] > MAX_PLANO and planos[i].get("ovs"):
                fin = max(planos[i]["t0"] + o.get("d", 0) + o["dur"] for o in planos[i]["ovs"])
                t2 = next((s for s in starts if s >= fin + 0.1), None)
                if t2 and t1 - t2 >= MIN_PLANO and t2 - planos[i]["t0"] >= MIN_PLANO:
                    planos.insert(i + 1, {"t0": round(t2, 3), "par": pi, "tipo": "img", "src": planos[i]["src"], "auto": True,
                                          "mov": segunda_toma(semilla, i + 1), "credito": planos[i]["credito"]})
            i += 1
        if pi and pi % 3 == 0 and len(planos) > primer and "trans" not in planos[primer]:
            planos[primer]["trans"], planos[primer]["tdur"] = "fundido", 0.4

        # 4) subtitulos exactos por palabra (<= 9 palabras / 2 lineas, cortando en frase)
        cur = []
        for j, tk in enumerate(toks):
            cur.append(j)
            txt = " ".join(toks[x] for x in cur)
            if tk.endswith((".", "?", "!", "…", ":", ";")) or len(cur) >= 9 or len(txt) > 48 or j == len(toks) - 1:
                ini = starts[cur[0]]
                fin = (starts[j + 1] if j + 1 < len(toks) else t_fin) - 0.04
                limpio = txt.replace("…", "").strip()
                if len(limpio) > 32:
                    pal = limpio.split()
                    corte = min(range(1, len(pal)), key=lambda q: abs(len(" ".join(pal[:q])) - len(" ".join(pal[q:]))))
                    limpio = " ".join(pal[:corte]) + "\n" + " ".join(pal[corte:])
                subs.append({"ini": round(ini, 3), "fin": round(max(fin, ini + 0.4), 3), "txt": limpio})
                cur = []
        base += len(toks)

    for k, pl in enumerate(planos):
        pl["t1"] = planos[k + 1]["t0"] if k + 1 < len(planos) else round(dur_total + 1.2, 3)
        for o in pl.get("ovs", []):
            o["dur"] = max(0.4, min(o["dur"], pl["t1"] - pl["t0"] - o.get("d", 0)))
        if pl.get("trans") == "barrido":
            sfx.append({"t": pl["t0"] - 0.18, "k": "whoosh"})
        elif pl.get("trans") == "fundido":
            sfx.append({"t": pl["t0"] - 0.25, "k": "whoosh_suave"})
    if planos:
        planos[0]["negro_in"] = 0.5
        planos[-1]["negro_out"] = 1.2
        if lugar.get("pais") and not any(o["k"] == "placa" for o in planos[0].get("ovs", [])):
            l1 = ", ".join(x for x in (lugar.get("region") or lugar.get("nombre"), lugar.get("pais")) if x).upper()
            planos[0].setdefault("ovs", []).append({"k": "placa", "l1": l1[:40], "l2": "", "d": 0.4, "dur": 4.0})
    sfx.insert(0, {"t": 0, "k": "ambiente", "dur": dur_total + 1.2, "tipo": plan.get("ambiente", "aire")})
    subs_por_plano = {}
    for si, s in enumerate(subs):
        for k, pl in enumerate(planos):
            if pl["t0"] < s["fin"] and pl["t1"] > s["ini"]:
                subs_por_plano.setdefault(str(k), []).append(si)
    creditos = [f"{f['titulo'][5:]} — {f['autor'] or 'autor en Commons'} — {f['licencia']} — {f['pagina']}" for f in commons.fichas.values()]
    tl = {"dur": round(dur_total + 1.2, 3), "planos": planos, "subs": subs, "subs_por_plano": subs_por_plano,
          "sfx": sorted(sfx, key=lambda e: e["t"])}
    return tl, creditos, rutas


def resumen(tl):
    d = [p["t1"] - p["t0"] for p in tl["planos"]]
    tipos, ovs = {}, {}
    for p in tl["planos"]:
        c = p.get("credito", "")
        k = p["tipo"] if p["tipo"] != "img" else ("foto" if "Foto:" in c else ("mapa" if c.startswith("Mapa") else "ia"))
        tipos[k] = tipos.get(k, 0) + 1
        for o in p.get("ovs", []):
            ovs[o["k"]] = ovs.get(o["k"], 0) + 1
    return {"planos": len(d), "seg_por_plano": round(sum(d) / max(len(d), 1), 2),
            "max_plano": round(max(d), 1) if d else 0, "tipos": tipos, "superposiciones": ovs}
