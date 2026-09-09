"""Tests del teaser con capa de texto.

El teaser dibujaba su texto con PIL y repartia los fragmentos de cada frase en
trozos de duracion IDENTICA, asi que el texto se despegaba de la voz: "¿Por qué
desaparece el comercio—" y "que las ciudades?" estaban el mismo tiempo en
pantalla aunque no se tarde lo mismo en decirlos. Marc lo vio en el primer
render del video 7: "no tiene absolutamente nada que ver con lo que dice".
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import render
from text_layer.ass_builder import S_C_TEASER, build_ass
from text_layer.schema import parse_text_layer
from text_layer.alignment import Word

FRASES = [
    {"fragmentos": ["¿Por qué desaparece el comercio—", "décadas antes—",
                    "que las ciudades?"], "voice_dur": 4.0},
    {"fragmentos": ["Este vídeo—", "tiene todas—", "las respuestas."], "voice_dur": 3.0},
]

# Tiempos "reales" de la voz: el primer fragmento se tarda mucho mas que el
# ultimo, que es justo lo que el reparto uniforme no recogia.
TRAMOS = {
    0: [(0.0, 2.2), (2.2, 3.0), (3.0, 3.9)],
    1: [(0.0, 0.6), (0.6, 1.3), (1.3, 2.9)],
}


def _teaser(voiced=True):
    frases = []
    for f in FRASES:
        frases.append({"fragmentos": list(f["fragmentos"]),
                       "voice_dur": f["voice_dur"] if voiced else None,
                       "voice_path": "/tmp/x.mp3" if voiced else None,
                       "cut": (f["voice_dur"] + render.TEASER_GAP) / 3})
    total = sum(f["voice_dur"] + render.TEASER_GAP for f in FRASES) + render.TEASER_FREEZE
    return {"frases": frases, "voiced": voiced, "total": round(total, 2)}


class TestSincronizacion(unittest.TestCase):
    def _cues(self, voiced=True):
        teaser = _teaser(voiced)
        with mock.patch.object(render, "_tramos_por_voz",
                               side_effect=lambda p, f, w, i: TRAMOS[i]):
            return teaser, render._teaser_cues(teaser, "/tmp")

    def test_cada_fragmento_sale_cuando_se_dice(self):
        teaser, cues = self._cues()
        self.assertEqual(len(cues), 6)
        # Bloque 1 empieza en 0; bloque 2 tras 4,0 + gap.
        base2 = FRASES[0]["voice_dur"] + render.TEASER_GAP
        self.assertAlmostEqual(cues[0].start, 0.0, places=2)
        self.assertAlmostEqual(cues[1].start, 2.2, places=2)
        self.assertAlmostEqual(cues[3].start, base2 + 0.0, places=2)
        self.assertAlmostEqual(cues[4].start, base2 + 0.6, places=2)

    def test_las_duraciones_dejan_de_ser_iguales(self):
        _, cues = self._cues()
        duraciones = [round(c.end - c.start, 2) for c in cues[:3]]
        self.assertEqual(len(set(duraciones)), 3,
                         f"siguen siendo uniformes: {duraciones}")

    def test_los_cortes_de_imagen_suman_el_bloque(self):
        teaser, _ = self._cues()
        for frase, origen in zip(teaser["frases"], FRASES):
            bloque = origen["voice_dur"] + render.TEASER_GAP
            self.assertAlmostEqual(sum(frase["frag_durs"]), bloque, places=2,
                                   msg="el video del teaser se saldria de su voz")

    def test_no_se_pisan_ni_se_salen(self):
        teaser, cues = self._cues()
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a.end, b.start + 1e-6, f"{a.id} pisa a {b.id}")
        for c in cues:
            self.assertLessEqual(c.end, teaser["total"] + 1e-6)

    def test_el_cierre_va_en_ambar(self):
        _, cues = self._cues()
        self.assertTrue(all(c.accent for c in cues[3:]), "el cierre tiene que ir en ambar")
        self.assertFalse(any(c.accent for c in cues[:3]))

    def test_sin_voz_se_reparte_por_igual(self):
        teaser = _teaser(voiced=False)
        cues = render._teaser_cues(teaser, "/tmp")
        duraciones = [round(c.end - c.start, 2) for c in cues[:3]]
        self.assertEqual(len(set(duraciones)), 1, "sin voz el reparto es uniforme")

    def test_si_whisper_falla_no_tira_el_render(self):
        teaser = _teaser()
        with mock.patch.object(render, "_tramos_por_voz",
                               side_effect=RuntimeError("whisper roto")):
            cues = render._teaser_cues(teaser, "/tmp")
        self.assertEqual(len(cues), 6, "tiene que caer al reparto uniforme, no romperse")


class TestEstilo(unittest.TestCase):
    def test_el_teaser_usa_su_propio_estilo_y_cabe(self):
        teaser = _teaser()
        with mock.patch.object(render, "_tramos_por_voz",
                               side_effect=lambda p, f, w, i: TRAMOS[i]):
            cues = render._teaser_cues(teaser, "/tmp")
        self.assertTrue(all(c.style_hint == "teaser" for c in cues))
        # A 211 px (titular centrado) el fragmento largo no cabria: 3.016 px.
        layer = parse_text_layer({"version": 1, "cues": [
            {"id": "z", "type": "C", "start": 9999.0, "end": 10001.0,
             "lines": ["X"], "accent": "X"}],
            "b_style": {"max_lines": 2, "max_chars_per_line": 42}})
        ass, rep = build_ass(layer, [Word("x", 0.0, 1.0)], pre_cues=cues)
        self.assertEqual(rep["teaser_cues"], 6)
        self.assertIn("Style: CTEASER,Anton,", ass)
        self.assertIn("CTEASER", ass)

    def test_el_estilo_del_teaser_no_lleva_borde(self):
        campos = S_C_TEASER.to_ass().split(",")
        self.assertEqual(float(campos[16]), 0.0)   # Outline
        self.assertEqual(float(campos[17]), 0.0)   # Shadow
        self.assertEqual(S_C_TEASER.size, 177)


if __name__ == "__main__":
    unittest.main(verbosity=2)
