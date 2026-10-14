"""Modo de rotación de audio_mix (14/10/2026): con una biblioteca de un solo mood, las
pistas se alternan ENTERAS, en orden fijo, hasta cubrir el vídeo.

Lo que se protege:
1. Nunca suena la misma pista en dos tramos seguidos (la queja era que no encajaba;
   con 3 pistas, repetir seguido se nota en seguida).
2. Cubre el vídeo entero, sin huecos ni solapes.
3. Cada pista suena casi entera (no se recorta a los tramos del plan musical).
4. El vídeo no arranca con la misma pista que el anterior.
5. Con la misma semilla, el mismo resultado (un re-render suena igual).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_mix as am

PISTAS = [
    {"id": "mystery_medium_01", "path": "/x/1.mp3", "mood": "mystery", "intensity": "medium", "duration": 240.7},
    {"id": "mystery_medium_02", "path": "/x/2.mp3", "mood": "mystery", "intensity": "medium", "duration": 144.5},
    {"id": "mystery_medium_03", "path": "/x/3.mp3", "mood": "mystery", "intensity": "medium", "duration": 527.0},
]
PLAN = [{"start": 0.0, "end": 300.0, "mood": "tension", "intensity": "high"},
        {"start": 300.0, "end": 900.0, "mood": "ancient", "intensity": "low"},
        {"start": 900.0, "end": 1380.0, "mood": "discovery", "intensity": "medium"}]


class TestRotacion(unittest.TestCase):
    def elegir(self, semilla="recABC", avoid=None):
        return am.select_tracks(PISTAS, PLAN, avoid=avoid or [], seed=semilla)

    def test_nunca_repite_seguida(self):
        for semilla in ("recA", "recB", "recC", "recD", "recE", "recF"):
            out = self.elegir(semilla)
            ids = [s["track"]["id"] for s in out]
            self.assertTrue(all(a != b for a, b in zip(ids, ids[1:])), ids)

    def test_cubre_todo_sin_huecos(self):
        out = self.elegir()
        self.assertEqual(out[0]["start"], 0.0)
        self.assertEqual(out[-1]["end"], 1380.0)
        for a, b in zip(out, out[1:]):
            self.assertAlmostEqual(a["end"], b["start"])

    def test_cada_pista_casi_entera(self):
        out = self.elegir()
        for s in out[:-1]:
            dur = s["track"]["duration"]
            self.assertGreaterEqual(s["end"] - s["start"], dur - am.BED_XFADE - 1.5)
            self.assertLessEqual(s["end"] - s["start"], dur)

    def test_no_arranca_como_el_anterior(self):
        for semilla in ("recA", "recB", "recC", "recD", "recE", "recF"):
            primera = self.elegir(semilla)[0]["track"]["id"]
            otra = self.elegir(semilla, avoid=[primera])[0]["track"]["id"]
            self.assertNotEqual(primera, otra)

    def test_determinista(self):
        a = [s["track"]["id"] for s in self.elegir("recXYZ")]
        b = [s["track"]["id"] for s in self.elegir("recXYZ")]
        self.assertEqual(a, b)

    def test_intensidad_sale_del_plan(self):
        out = self.elegir()
        self.assertEqual(out[0]["intensity"], "high")      # empieza en el tramo 0-300 del plan

    def test_varios_moods_sigue_el_modo_por_plan(self):
        mezcla = PISTAS + [{"id": "tension_low_01", "path": "/x/t.mp3", "mood": "tension", "intensity": "low", "duration": 200}]
        out = am.select_tracks(mezcla, PLAN, avoid=[], seed="recA")
        self.assertEqual(len(out), len(PLAN))              # un tramo por tramo del plan, como antes


if __name__ == "__main__":
    unittest.main()
