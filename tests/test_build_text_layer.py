"""Tests del script auxiliar que genera la capa con Claude.

No se llama a la API: lo que se prueba es lo que puede fallar sin ella, que
es el tratamiento de la respuesta. Claude devuelve JSON entre backticks a
menudo, y el encargo exige limpiarlo antes de parsear y rechazar la capa si
no cumple el esquema.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from build_text_layer import SYSTEM, _clean_json
from text_layer.schema import TextLayerError, parse_text_layer

VALIDO = {"version": 1,
          "cues": [{"id": "c01", "type": "C", "anchor_text": "Nadie volverá a verla con vida.",
                    "lines": ["NADIE VOLVERÁ", "A VERLA CON VIDA"], "accent": "CON VIDA"}],
          "b_style": {"max_lines": 2, "max_chars_per_line": 42}}


class TestLimpiezaDeLaRespuesta(unittest.TestCase):
    def test_json_pelado(self):
        self.assertEqual(_clean_json(json.dumps(VALIDO)), VALIDO)

    def test_json_entre_backticks(self):
        self.assertEqual(_clean_json("```json\n" + json.dumps(VALIDO) + "\n```"), VALIDO)

    def test_json_con_texto_alrededor(self):
        crudo = ("Aquí tienes la capa de texto:\n\n```\n" + json.dumps(VALIDO)
                 + "\n```\n\nEspero que te sirva.")
        self.assertEqual(_clean_json(crudo), VALIDO)

    def test_respuesta_sin_json(self):
        with self.assertRaises(ValueError):
            _clean_json("No he podido generar la capa de texto.")


class TestLaSalidaSeValida(unittest.TestCase):
    def test_una_capa_valida_pasa(self):
        parse_text_layer(_clean_json(json.dumps(VALIDO)))

    def test_una_capa_invalida_se_rechaza(self):
        malo = json.loads(json.dumps(VALIDO))
        malo["cues"][0]["accent"] = "SIN RASTRO"
        with self.assertRaises(TextLayerError):
            parse_text_layer(_clean_json(json.dumps(malo)))


class TestElPromptCubreLasReglas(unittest.TestCase):
    def test_estan_las_reglas_editoriales_del_encargo(self):
        for regla in ["1 / 3", "B_LOOP", "background", "black", "anchor_text",
                      "LITERAL", "MAYÚSCULAS", "counter", "placement"]:
            self.assertIn(regla, SYSTEM, f"el prompt no menciona {regla!r}")

    def test_pide_los_tres_estilos_y_solo_esos(self):
        self.assertIn("C · golpe", SYSTEM)
        self.assertIn("E · rótulo de dato", SYSTEM)
        self.assertIn("E_Q · rótulo de incógnita", SYSTEM)
        self.assertIn("El subtítulo normal (estilo B) NO se declara", SYSTEM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
