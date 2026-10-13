"""Del plan visual de Claude + los tiempos reales de la voz a una linea de tiempo de planos.

El plan va por PARRAFOS del guion (el mismo texto que leyo la voz). Para cada parrafo
el planificador decide cuantos planos caben (uno cada ~3,4 s, el ritmo del piloto),
corta en el inicio de palabra mas cercano y reparte el material del plan en orden.
"""
import hashlib
import logging
import os
import re

from . import activos

log = logging.getLogger("render_v2.plan")
SEG_POR_PLANO = 3.4
BREAK = re.compile(r"<break[^>]*/?>")


def _norm(s):
    s = s.lower()
    for a, b in zip("áéíóúüñ", "aeiouun"):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", s)


def parrafos_de(script_text):
    pars = [BREAK.sub(" ", p).strip() for p in re.split(r"\n\s*\n", script_text or "")]
    return [p for p in pars if p]


def _rnd(semilla, i, n):
    h = hashlib.md5(f"{semilla}:{i}".encode()).hexdigest()
    return int(h[:8], 16) % n


def movimiento(semilla, i):
    """Movimiento siempre visible (10-18 %), variado y repetible para un mismo video."""
    k = _rnd(semilla, i, 6)
    a = 0.10 + 0.08 * _rnd(semilla, i + 999, 100) / 100
    if k == 0:
        return {"cx": [.5, .5], "cy": [.5, .5], "z": [1.0, 1.0 - a]}
    if k == 1:
        return {"cx": [.5, .5], "cy": [.5, .5], "z": [1.0 - a, 1.0]}
    if k == 2:
        return {"cx": [.5 - a / 1.5, .5 + a / 1.5], "cy": [.5, .5], "z": [.8, .8]}
    if k == 3:
        return {"cx": [.5 + a / 1.5, .5 - a / 1.5], "cy": [.5, .5], "z": [.8, .8]}
    if k == 4:
        return {"cx": [.45, .55], "cy": [.55, .45], "z": [.95, .95 - a]}
    return {"cx": [.55, .45], "cy": [.45, .55], "z": [.85, .85 + a]}


def construir(script_text, palabras, plan, dur_total, commons, ia, carpeta_mapas, semilla,
              fuente_titulo=None):
    """palabras: lista de Word(word, start, end) alineada con los tokens del guion.
    ia: {"ITEM_1_IMG_1": ruta_local, ...}. Devuelve (timeline, creditos, rutas)."""
    pars = parrafos_de(script_text)
    toks_par = [p.split() for p in pars]
    n_tok = sum(len(t) for t in toks_par)
    rutas = dict(ia)
    # Tiempos por token. Si la alineacion trae otro numero de tokens, se reparte en proporcion.
    def tiempo(idx, campo="start"):
        if not palabras:
            return dur_total * idx / max(n_tok, 1)
        j = min(len(palabras) - 1, int(idx * len(palabras) / max(n_tok, 1))) if len(palabras) != n_tok else min(idx, len(palabras) - 1)
        return getattr(palabras[j], campo)

    plan_pars = {int(p.get("p", i)): p for i, p in enumerate(plan.get("parrafos") or [])}
    lugar = plan.get("lugar_principal") or {}
    planos, subs, sfx = [], [], []
    ia_libres = sorted(ia)
    ia_usadas = set()

    def ia_cualquiera(preferida=None):
        if preferida and preferida in ia:
            ia_usadas.add(preferida)
            return preferida
        libres = [k for k in ia_libres if k not in ia_usadas] or ia_libres
        if not libres:
            # Sin reserva de IA: se reutiliza una foto o mapa ya usado (con otro movimiento)
            # antes que dejar un hueco, que alargaria el plano anterior y rompe el ritmo.
            vistos = [pl["src"] for pl in planos if pl.get("tipo") == "img" and pl.get("src")]
            return vistos[_rnd(semilla, len(planos), len(vistos))] if vistos else None
        k = libres[_rnd(semilla, len(planos), len(libres))]
        ia_usadas.add(k)
        return k

    mapas_hechos = {}
    ultimo_fondo = None
    base = 0
    for pi, toks in enumerate(toks_par):
        t_ini = tiempo(base) if pi else 0.0
        t_fin = tiempo(base + len(toks)) if pi + 1 < len(toks_par) else dur_total
        starts = [tiempo(base + k) for k in range(len(toks))]
        pp = plan_pars.get(pi, {})
        material = [m for m in (pp.get("planos") or []) if isinstance(m, dict)] or [{"tipo": "ia"}]
        D = max(0.5, t_fin - t_ini)
        n = max(1, round(D / SEG_POR_PLANO))
        cortes = [t_ini]
        for k in range(1, n):
            objetivo = t_ini + D * k / n
            cerca = min(starts, key=lambda s: abs(s - objetivo)) if starts else objetivo
            c = cerca if abs(cerca - objetivo) < 0.7 else objetivo
            if c - cortes[-1] > 1.2:
                cortes.append(c)
        primer_plano_par = len(planos)
        unicos_usados = set()
        for k, c0 in enumerate(cortes):
            m = material[k % len(material)]
            tipo = (m.get("tipo") or "ia").lower()
            # titulo, cita y mapa no se repiten dentro del mismo parrafo: al reciclar el
            # material se pasa al siguiente, y si no queda otro, a una imagen de reserva.
            if tipo in ("titulo", "cita", "mapa"):
                if id(m) in unicos_usados:
                    alternativas = [x for x in material if (x.get("tipo") or "ia").lower() not in ("titulo", "cita", "mapa")]
                    m = alternativas[k % len(alternativas)] if alternativas else {"tipo": "ia"}
                    tipo = (m.get("tipo") or "ia").lower()
                else:
                    unicos_usados.add(id(m))
            pl = {"t0": round(c0, 3), "par": pi}
            clave = None
            if tipo == "foto":
                consultas = m.get("buscar") or []
                if isinstance(consultas, str):
                    consultas = [consultas]
                clave = commons.siguiente(consultas) if consultas else None
                if clave:
                    rutas[clave] = commons.fichas[clave]["archivo"]
                    f = commons.fichas[clave]
                    lugar_txt = m.get("pie") or ""
                    pl["credito"] = ((lugar_txt + " · ") if lugar_txt else "") + f"Foto: {f['autor'] or 'Wikimedia Commons'} · {f['licencia']}"
                else:
                    clave = ia_cualquiera(m.get("ia"))
                    pl["credito"] = "Recreación artística (IA)"
                pl.update(tipo="img", src=clave, mov=movimiento(semilla, len(planos)))
            elif tipo == "mapa" and activos.geo_disponible():
                try:
                    lat, lon = float(m["lat"]), float(m["lon"])
                    key = (round(lat, 2), round(lon, 2), m.get("etiqueta", ""))
                    if key not in mapas_hechos:
                        clave = f"mapa{len(mapas_hechos):02d}"
                        destino = os.path.join(carpeta_mapas, clave + ".jpg")
                        punto = activos.mapa(destino, lat, lon, (m.get("etiqueta") or "").upper(),
                                             region=m.get("region"), radio=float(m.get("radio", 3.2)),
                                             fuente_titulo=fuente_titulo)
                        mapas_hechos[key] = (clave, punto)
                        rutas[clave] = destino
                    clave, punto = mapas_hechos[key]
                    pl.update(tipo="img", src=clave, credito="Mapa: Natural Earth (dominio público) · elaboración propia",
                              mov={"cx": [.5, punto["cx"]], "cy": [.5, punto["cy"]], "z": [.75, .32]})
                except Exception as e:
                    log.warning("Mapa no generado (%s): uso IA", e)
                    clave = ia_cualquiera()
                    pl.update(tipo="img", src=clave, mov=movimiento(semilla, len(planos)), credito="Recreación artística (IA)")
            elif tipo in ("titulo", "cita") and ultimo_fondo:
                pl.update(tipo=tipo, fondo=ultimo_fondo, texto=str(m.get("texto", ""))[:120],
                          sub=m.get("sub", ""), autor=m.get("autor", ""), flash=(tipo == "titulo"))
                if tipo == "titulo":
                    sfx += [{"t": c0 - 2.6, "k": "riser"}, {"t": c0, "k": "impacto"}]
            else:
                clave = ia_cualquiera(m.get("img") or m.get("ia"))
                pl.update(tipo="img", src=clave, mov=movimiento(semilla, len(planos)), credito="Recreación artística (IA)")
            if pl.get("tipo") == "img" and pl.get("src") and pl["src"] not in ia:
                previo = next((x for x in planos if x.get("src") == pl["src"] and x.get("credito")), None)
                if previo and pl.get("credito") == "Recreación artística (IA)":
                    pl["credito"] = previo["credito"]
            if pl["tipo"] == "img":
                if not pl.get("src"):
                    continue
                ultimo_fondo = pl["src"]
            planos.append(pl)
        # superposiciones del parrafo
        norm_toks = [_norm(t) for t in toks]
        for ov in (pp.get("superpuestos") or []):
            k = (ov.get("tipo") or "").lower()
            t_ov = t_ini + 0.3
            if ov.get("frase"):
                clave_n = _norm(str(ov["frase"]).split()[0])
                for j, nt in enumerate(norm_toks):
                    if clave_n and nt.startswith(clave_n):
                        t_ov = starts[j]
                        break
            destino_i = max((i for i in range(primer_plano_par, len(planos)) if planos[i]["t0"] <= t_ov + 1e-6),
                            default=primer_plano_par) if len(planos) > primer_plano_par else None
            if destino_i is None:
                continue
            pl = planos[destino_i]
            d = max(0.0, t_ov - pl["t0"])
            if k == "placa":
                o = {"k": "placa", "l1": str(ov.get("l1", "")).upper()[:40], "l2": str(ov.get("l2", ""))[:50], "d": d, "dur": 4.2}
            elif k == "cifra":
                o = {"k": "cifra", "grande": str(ov.get("grande", ""))[:14], "peq": str(ov.get("peq", "")).upper()[:40], "d": d, "dur": 3.4}
                sfx.append({"t": t_ov, "k": "impacto_suave"})
            elif k == "nombre":
                o = {"k": "nombre", "n": str(ov.get("n", "")).upper()[:32], "r": str(ov.get("r", ""))[:60], "d": d, "dur": 4.0}
            elif k == "capitulo":
                pl0 = planos[primer_plano_par]
                pl0["trans"], pl0["tdur"] = "barrido", 0.25
                pl0.setdefault("ovs", []).append({"k": "capitulo", "num": str(ov.get("num", "")), "txt": str(ov.get("txt", "")).upper()[:28], "d": 0, "dur": 1.6})
                sfx.append({"t": pl0["t0"], "k": "impacto_suave"})
                continue
            else:
                continue
            pl.setdefault("ovs", []).append(o)
        if pi and pi % 3 == 0 and len(planos) > primer_plano_par and "trans" not in planos[primer_plano_par]:
            planos[primer_plano_par]["trans"] = "fundido"
        # subtitulos exactos por palabra: trozos de <= 10 palabras / 60 caracteres, cortando en frase
        cur = []
        for j, tk in enumerate(toks):
            cur.append(j)
            fin_frase = tk.endswith((".", "?", "!", "…", ":", ";"))
            txt = " ".join(toks[x] for x in cur)
            if fin_frase or len(cur) >= 10 or len(txt) > 52 or j == len(toks) - 1:
                ini = starts[cur[0]]
                fin = (starts[j + 1] if j + 1 < len(toks) else t_fin) - 0.05
                limpio = txt.replace("…", "").strip()
                if len(limpio) > 34:
                    pal = limpio.split()
                    corte = min(range(1, len(pal)), key=lambda q: abs(len(" ".join(pal[:q])) - len(" ".join(pal[q:]))))
                    limpio = " ".join(pal[:corte]) + "\n" + " ".join(pal[corte:])
                subs.append({"ini": round(ini, 3), "fin": round(max(fin, ini + 0.4), 3), "txt": limpio})
                cur = []
        base += len(toks)

    # cierre de tiempos, transiciones y efectos
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
    creditos = []
    for clave, f in commons.fichas.items():
        creditos.append(f"{f['titulo'][5:]} — {f['autor'] or 'autor en Commons'} — {f['licencia']} — {f['pagina']}")
    tl = {"dur": round(dur_total + 1.2, 3), "planos": planos, "subs": subs, "subs_por_plano": subs_por_plano,
          "sfx": sorted(sfx, key=lambda e: e["t"])}
    return tl, creditos, rutas


def resumen(tl):
    d = [p["t1"] - p["t0"] for p in tl["planos"]]
    tipos = {}
    for p in tl["planos"]:
        c = p.get("credito", "")
        k = "foto" if c.startswith(("Foto", )) or " · Foto:" in c else ("mapa" if c.startswith("Mapa") else ("ia" if "IA" in c else p["tipo"]))
        tipos[k] = tipos.get(k, 0) + 1
    return {"planos": len(d), "seg_por_plano": round(sum(d) / max(len(d), 1), 2),
            "max_plano": round(max(d), 1) if d else 0, "tipos": tipos}
