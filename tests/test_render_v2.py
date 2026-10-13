"""Tests del render v2 (estilo piloto Valeriana) sin red: planificador y motor."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from render_v2 import motor, planificador
from text_layer.alignment import Word

GUION = ("Por esta carretera de Campeche pasan camiones cada dia. Y justo al lado hay una ciudad maya entera.\n\n"
         "Nadie la tenia en ningun mapa…\n\n"
         "En 2024 la encuentra un estudiante llamado Luke Auld-Thomas. Salen seis mil setecientas sesenta y cuatro "
         "estructuras en total, una por una, contadas con paciencia durante meses de trabajo.\n\n"
         "Le ponen un nombre: Valeriana.")


def palabras(texto, ppm=136):
    toks = texto.split()
    paso = 60 / ppm
    return [Word(t, i * paso, i * paso + paso * 0.9) for i, t in enumerate(toks)], len(toks) * paso


class CommonsFalso:
    """Devuelve fotos locales sinteticas en vez de ir a Wikimedia."""

    def __init__(self, carpeta, n=6):
        self.fichas = {}
        self.carpeta = carpeta
        self.n = n

    def siguiente(self, consultas):
        if len(self.fichas) >= self.n:
            return None
        clave = f"cm{len(self.fichas):03d}"
        ruta = os.path.join(self.carpeta, clave + ".jpg")
        Image.new("RGB", (2400, 1350), (40 * len(self.fichas) % 255, 120, 90)).save(ruta)
        self.fichas[clave] = {"archivo": ruta, "titulo": f"File:Foto {clave}.jpg", "autor": "Autor",
                              "licencia": "CC BY 4.0", "pagina": "https://commons.wikimedia.org/x"}
        return clave


PLAN = {"lugar_principal": {"nombre": "Valeriana", "region": "Campeche", "pais": "México"},
        "parrafos": [
            {"p": 0, "planos": [{"tipo": "foto", "buscar": ["Highway 186 Campeche"], "pie": "Carretera 186 (México)"}],
             "superpuestos": [{"tipo": "placa", "l1": "Campeche, México"}]},
            {"p": 1, "planos": [{"tipo": "ia", "img": "ITEM_1_IMG_1"}]},
            {"p": 2, "planos": [{"tipo": "foto", "buscar": ["Tulane"]}, {"tipo": "ia"}],
             "superpuestos": [{"tipo": "nombre", "n": "Luke Auld-Thomas", "r": "Tulane", "frase": "Auld-Thomas"},
                              {"tipo": "cifra", "grande": "6.764", "peq": "estructuras", "frase": "seis"},
                              {"tipo": "capitulo", "num": "1", "txt": "La pagina 16"}]},
            {"p": 3, "planos": [{"tipo": "titulo", "texto": "VALERIANA"}]}]}


class TestPlanificador(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ia = {}
        for k in ("ITEM_1_IMG_1", "ITEM_1_IMG_2", "ATM_1"):
            r = os.path.join(self.tmp, k + ".png")
            Image.new("RGB", (1600, 900), (200, 150, 90)).save(r)
            self.ia[k] = r
        self.words, self.dur = palabras(GUION)

    def construir(self, commons=None, ia=None):
        commons = commons or CommonsFalso(self.tmp)
        return planificador.construir(GUION, self.words, PLAN, self.dur, commons, self.ia if ia is None else ia,
                                      self.tmp, "semilla")

    def test_ritmo_y_continuidad(self):
        tl, creditos, rutas = self.construir()
        planos = tl["planos"]
        self.assertGreater(len(planos), 3)
        for a, b in zip(planos, planos[1:]):
            self.assertAlmostEqual(a["t1"], b["t0"], places=3)          # sin huecos
            self.assertGreater(a["t1"] - a["t0"], 1.0)
        media = sum(p["t1"] - p["t0"] for p in planos) / len(planos)
        self.assertLess(media, 6.5)                                     # ~5 s por plano
        self.assertEqual(planos[0]["t0"], 0.0)

    def test_titulo_una_vez_y_superposiciones(self):
        tl, _, _ = self.construir()
        self.assertEqual(sum(p["tipo"] == "titulo" for p in tl["planos"]), 1)
        ovs = [o["k"] for p in tl["planos"] for o in p.get("ovs", [])]
        for k in ("placa", "nombre", "cifra", "capitulo"):
            self.assertIn(k, ovs)
        self.assertTrue(any(p.get("trans") == "barrido" for p in tl["planos"]))

    def test_subtitulos_cubren_y_son_del_guion(self):
        tl, _, _ = self.construir()
        texto = " ".join(s["txt"].replace("\n", " ") for s in tl["subs"])
        self.assertIn("Valeriana", texto)
        for s in tl["subs"]:
            self.assertLessEqual(len(s["txt"].split("\n")), 2)
            self.assertGreater(s["fin"], s["ini"])

    def test_creditos_y_sin_ia_no_hay_huecos(self):
        tl, creditos, rutas = self.construir(commons=CommonsFalso(self.tmp, n=2), ia={})
        self.assertEqual(len(creditos), 2)
        self.assertTrue(all(p.get("src") for p in tl["planos"] if p["tipo"] == "img"))
        self.assertTrue(all(p.get("credito") != "Recreación artística (IA)" for p in tl["planos"]))

    def test_motor_dibuja_un_fotograma(self):
        tl, _, rutas = self.construir()
        motor.RUTAS.clear()
        motor.RUTAS.update(rutas)
        for t in (0.6, tl["dur"] / 2, tl["dur"] - 0.6):
            fr = motor.fotograma(tl, t)
            self.assertEqual(fr.size, (motor.W, motor.H))


if __name__ == "__main__":
    unittest.main()


class TestMiniaturaV3(unittest.TestCase):
    def test_reglas_de_la_miniatura(self):
        import thumbnail_v3
        tmp = tempfile.mkdtemp()
        oscura = os.path.join(tmp, "f.png")
        Image.new("RGB", (1600, 900), (25, 30, 35)).save(oscura)
        for estilo in ("flecha", "lupa", "split", "texto"):
            out = os.path.join(tmp, estilo + ".png")
            m = thumbnail_v3.componer(oscura, out, "una ciudad perdida", "bajo la selva", estilo=estilo, fondo2=oscura)
            self.assertEqual(Image.open(out).size, (1280, 720))
            self.assertLessEqual(len(" ".join(m["texto"]).split()), 3)          # 3 palabras como mucho
            self.assertGreaterEqual(m["letra_a_168px"], 12)                    # legible en el movil
            self.assertGreaterEqual(m["luma"], thumbnail_v3.LUMA_MIN - 10)     # luminosa aunque el fondo no lo sea


class TestSincroniaV3(unittest.TestCase):
    """El acabado de la v3: la imagen y los efectos entran en la palabra exacta."""

    def test_anclas_por_palabra(self):
        tmp = tempfile.mkdtemp()
        ia = {}
        for k in ("ITEM_1_IMG_1", "ITEM_1_IMG_2", "ITEM_2_IMG_1"):
            r = os.path.join(tmp, k + ".png")
            Image.new("RGB", (1600, 900), (180, 140, 90)).save(r)
            ia[k] = r
        words, dur = palabras(GUION)
        plan = {"parrafos": [
            {"p": 0, "planos": [{"tipo": "ia", "img": "ITEM_1_IMG_1"}, {"tipo": "ia", "img": "ITEM_1_IMG_2", "desde": "ciudad"}],
             "superpuestos": [{"tipo": "marca", "texto": "Ciudad", "frase": "ciudad"}]},
            {"p": 2, "planos": [{"tipo": "ia", "img": "ITEM_2_IMG_1"}, {"tipo": "escaneo", "img": "ITEM_1_IMG_1", "desde": "estructuras"}],
             "superpuestos": [{"tipo": "cifra", "grande": "6.764", "peq": "estructuras", "frase": "seis"}]}]}
        tl, _, _ = planificador.construir(GUION, words, plan, dur, CommonsFalso(tmp), ia, tmp, "s")
        t_ciudad = next(w.start for w in words if w.word.lower().startswith("ciudad"))
        self.assertTrue(any(abs(p["t0"] - t_ciudad) < 1e-3 for p in tl["planos"]))   # corte en «ciudad»
        marcas = [(p, o) for p in tl["planos"] for o in p.get("ovs", []) if o["k"] == "marca"]
        self.assertEqual(len(marcas), 1)
        p, o = marcas[0]
        self.assertAlmostEqual(p["t0"] + o["d"], t_ciudad, places=3)                 # marca en «ciudad»
        # el escaneo parte de la imagen del plano anterior y revela otra distinta
        idx = [i for i, p in enumerate(tl["planos"]) if p["tipo"] == "scan"]
        self.assertEqual(len(idx), 1)
        self.assertNotEqual(tl["planos"][idx[0]]["a"], tl["planos"][idx[0]]["b"])
        cifra = [o for p in tl["planos"] for o in p.get("ovs", []) if o["k"] == "cifra"][0]
        self.assertTrue(cifra["contar"])
        self.assertGreaterEqual(cifra["dur"], 2.0)          # ninguna toma automatica la corta
        motor.RUTAS.clear(); motor.RUTAS.update(ia)
        for p in tl["planos"]:
            motor.fotograma(tl, (p["t0"] + p["t1"]) / 2)                               # todos los tipos dibujan
