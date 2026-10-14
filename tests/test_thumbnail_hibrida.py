"""Tests de la miniatura hibrida (escena de cine + lupa que revela).

Lo que se comprueba es lo que fallo en las pruebas a mano del 14/10/2026:
1. la lupa tapaba al heroe de la escena: tiene que ir donde hay MENOS detalle;
2. las antiguas eran oscuras (luma 46-63): la hibrida tiene que salir luminosa;
3. la ampliacion salia pixelada al sacarla de la miniatura de 1280: se saca del original a 2x.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image, ImageDraw

import thumbnail_hibrida as th


def _escena(ruta, heroe=(0.74, 0.62), oscura=True):
    """Fondo oscuro y liso con un 'heroe' muy detallado (tablero) en `heroe`."""
    rng = np.random.default_rng(1)
    a = np.full((768, 1376, 3), 40 if oscura else 150, np.float32) + rng.normal(0, 2, (768, 1376, 3))
    im = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    cx, cy = heroe[0] * 1376, heroe[1] * 768
    for i in range(-8, 8):
        for j in range(-8, 8):
            if (i + j) % 2:
                d.rectangle([cx + i * 16, cy + j * 16, cx + i * 16 + 15, cy + j * 16 + 15], fill=(240, 200, 120))
    im.save(ruta)
    return ruta


class TestHibrida(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_la_lupa_no_tapa_al_heroe(self):
        f = _escena(os.path.join(self.tmp, "f.png"), heroe=(0.74, 0.62))
        m = th.componer(f, os.path.join(self.tmp, "o.png"), "ciudad", "oculta")
        cx, cy = m["foco"][0] * th.W, m["foco"][1] * th.H
        hx, hy = 0.74 * th.W, 0.62 * th.H
        self.assertGreater(np.hypot(cx - hx, cy - hy), th.RADIO * 0.8)

    def test_luminosa_aunque_el_fondo_sea_oscuro(self):
        f = _escena(os.path.join(self.tmp, "f.png"))
        m = th.componer(f, os.path.join(self.tmp, "o.png"), "puerta", "sellada")
        self.assertGreaterEqual(m["luma"], 95)

    def test_salida_y_tres_palabras(self):
        f = _escena(os.path.join(self.tmp, "f.png"), oscura=False)
        out = os.path.join(self.tmp, "o.png")
        m = th.componer(f, out, "nadie puede leer", "este rollo")
        self.assertEqual(Image.open(out).size, (1280, 720))
        self.assertLessEqual(len(" ".join(m["texto"]).split()), 3)
        self.assertEqual(m["estilo"], "hibrida")

    def test_revela_fondo2_y_respeta_lupa_c(self):
        f = _escena(os.path.join(self.tmp, "f.png"))
        f2 = os.path.join(self.tmp, "f2.png")
        Image.new("RGB", (1376, 768), (0, 200, 255)).save(f2)
        out = os.path.join(self.tmp, "o.png")
        m = th.componer(f, out, "estaba", "ahi", fondo2=f2, lupa_c=(0.7, 0.5))
        self.assertEqual(m["foco"], [0.7, 0.5])
        px = np.asarray(Image.open(out).convert("RGB"))[int(0.5 * th.H) + 40, int(0.7 * th.W) + 40]
        self.assertGreater(int(px[2]), 150)        # dentro del cristal se ve la imagen oculta (cian)
        self.assertLess(int(px[0]), 120)

    def test_ampliacion_desde_el_original_a_2x(self):
        """El tablero ampliado conserva bordes nitidos: hay pixeles casi puros de los dos colores."""
        f = _escena(os.path.join(self.tmp, "f.png"), heroe=(0.5, 0.5))
        out = os.path.join(self.tmp, "o.png")
        m = th.componer(f, out, "", "", foco=(0.5, 0.5), lupa_c=(0.75, 0.6))
        a = np.asarray(Image.open(out).convert("L"), dtype=np.float32)
        cx, cy = int(m["foco"][0] * th.W), int(m["foco"][1] * th.H)
        dentro = a[cy - 80:cy + 80, cx - 80:cx + 80]
        self.assertGreater(dentro.std(), 40)


if __name__ == "__main__":
    unittest.main()
