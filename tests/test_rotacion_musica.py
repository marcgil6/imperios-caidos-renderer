"""Modo de rotación de audio_mix (14/10/2026, orden de Marc): con una biblioteca de un solo
mood, cada vídeo lleva UNA sola pista y los vídeos se van alternando entre las tres.

Lo que se protege:
1. Dentro de un vídeo no cambia la música: un único tramo que cubre todo el vídeo.
2. Cada vídeo usa la siguiente pista a la del vídeo anterior (01 -> 02 -> 03 -> 01).
3. El historial de EP-07 (`music_avoid`, lo más reciente primero) puede traer pistas ya
   retiradas: se saltan y cuenta la primera que siga en la biblioteca.
4. Sin historial útil decide la semilla, y con la misma semilla sale lo mismo
   (un re-render suena igual).
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


def elegir(avoid=None, semilla="recABC"):
    return am.select_tracks(PISTAS, PLAN, avoid=avoid or [], seed=semilla)


class TestRotacion(unittest.TestCase):
    def test_una_sola_pista_por_video(self):
        out = elegir()
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["start"], out[0]["end"]), (0.0, 1380.0))

    def test_sigue_a_la_del_video_anterior(self):
        for anterior, siguiente in (("mystery_medium_01", "mystery_medium_02"),
                                    ("mystery_medium_02", "mystery_medium_03"),
                                    ("mystery_medium_03", "mystery_medium_01")):
            self.assertEqual(elegir([anterior, "mystery_medium_01"])[0]["track"]["id"], siguiente)

    def test_salta_pistas_retiradas_del_historial(self):
        out = elegir(["tension_medium_02", "ancient_low_01", "mystery_medium_02"])
        self.assertEqual(out[0]["track"]["id"], "mystery_medium_03")

    def test_tres_videos_seguidos_usan_las_tres(self):
        historial, usadas = [], []
        for n in range(6):
            tid = elegir(historial, semilla=f"rec{n}")[0]["track"]["id"]
            usadas.append(tid)
            historial = [tid] + historial
        self.assertEqual(len(set(usadas[:3])), 3)
        self.assertTrue(all(a != b for a, b in zip(usadas, usadas[1:])))

    def test_sin_historial_decide_la_semilla(self):
        self.assertEqual(elegir([], "recXYZ")[0]["track"]["id"], elegir([], "recXYZ")[0]["track"]["id"])
        self.assertEqual(len({elegir([], f"rec{i}")[0]["track"]["id"] for i in range(20)}), 3)

    def test_varios_moods_sigue_el_modo_por_plan(self):
        mezcla = PISTAS + [{"id": "tension_low_01", "path": "/x/t.mp3", "mood": "tension", "intensity": "low", "duration": 200}]
        self.assertEqual(len(am.select_tracks(mezcla, PLAN, avoid=[], seed="recA")), len(PLAN))


if __name__ == "__main__":
    unittest.main()
