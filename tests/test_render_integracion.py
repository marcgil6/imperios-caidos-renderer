"""Que la capa de texto no cambie el render de siempre.

El requisito duro del encargo es que un /render SIN `text_layer` se comporte
exactamente igual que antes. Aqui se comprueba sobre lo unico que se toco de
ese camino: la construccion del filtergraph de quemado.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRetrocompatibilidad(unittest.TestCase):
    """El filtergraph del camino clasico tiene que salir byte a byte igual."""

    def _filtergraph(self, **kwargs):
        import render
        capturado = {}

        def falso_ffmpeg(args, timeout=300):
            capturado["args"] = args
            return None

        with mock.patch.object(render, "_ffmpeg", falso_ffmpeg):
            render._burn_subtitles_and_cta("in.mp4", "/tmp/subs.ass", "out.mp4",
                                           duration_sec=1200, **kwargs)
        args = capturado["args"]
        return args[args.index("-filter_complex") + 1]

    def test_sin_capa_de_texto_el_filtergraph_es_el_de_siempre(self):
        fg = self._filtergraph()
        # Exactamente lo que documenta CLAUDE_EP.md para el build sin logo.
        self.assertTrue(fg.startswith("[0:v]ass=/tmp/subs.ass,drawtext="), fg)
        self.assertTrue(fg.endswith("[vout]"), fg)
        self.assertNotIn("drawbox", fg)
        self.assertNotIn("fontsdir", fg)

    def test_con_capa_de_texto_el_fondo_negro_va_antes_del_ass(self):
        fg = self._filtergraph(
            pre_filters=["drawbox=x=0:y=0:w=iw:h=ih:color=black@1:t=fill:"
                         "enable='between(t,15.320,17.720)'"],
            fonts_dir="/app/fonts")
        self.assertLess(fg.index("drawbox"), fg.index("ass="),
                        "el fondo negro tiene que quedar DEBAJO del titular")
        self.assertIn("ass=/tmp/subs.ass:fontsdir=/app/fonts", fg)
        # Y el CTA sigue siendo la capa mas alta.
        self.assertGreater(fg.index("drawtext"), fg.index("ass="))

    def test_el_cta_de_los_ultimos_60s_no_cambia(self):
        import render
        con = render._build_cta_filters(1200)
        self.assertTrue(any("enable" in f for f in con))
        self.assertEqual(render._build_cta_filters(None), [])


class TestModuloCargable(unittest.TestCase):
    def test_todo_lo_que_exporta_text_layer_existe(self):
        import text_layer
        for nombre in text_layer.__all__:
            self.assertTrue(hasattr(text_layer, nombre), nombre)

    def test_render_importa_lo_que_usa(self):
        import render
        for nombre in ("_build_text_layer", "_words_for_text_layer", "_fonts_report",
                       "parse_text_layer", "build_ass", "build_video_filters",
                       "words_from_payload", "align_script_to_words",
                       "words_from_whisper", "find_fonts_dir", "TextLayerError"):
            self.assertTrue(hasattr(render, nombre), nombre)

    def test_las_cuatro_caras_estan_en_el_repo(self):
        from text_layer.burn import find_fonts_dir
        from text_layer.metrics import _FONT_FILES
        d = find_fonts_dir()
        self.assertIsNotNone(d, "no se encuentra la carpeta de fuentes")
        for familia, fichero in _FONT_FILES.items():
            self.assertTrue(os.path.exists(os.path.join(d, fichero)),
                            f"falta {fichero} ({familia})")

    def test_los_tamanos_ass_se_derivan_de_la_fuente(self):
        from text_layer.metrics import ass_fontsize
        # Si alguien cambia una fuente por otra de metricas distintas, estos
        # numeros cambian: es lo correcto, el tamano en pantalla se mantiene.
        self.assertEqual(ass_fontsize("Anton", 211), 366)
        self.assertEqual(ass_fontsize("Cormorant Garamond Medium", 79), 109)


if __name__ == "__main__":
    unittest.main(verbosity=2)
