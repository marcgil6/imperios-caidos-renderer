"""Tests del generador de la capa de texto (FASE 5.1 del encargo).

Cubren tres cosas: que el .ass sale con los eventos que toca, que el
subtitulo B se recorta bajo los cues C/E (nunca dos capas a la vez) y que
las validaciones fallan cuando tienen que fallar.
"""

import collections
import copy
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.gancho_amelia import SCRIPT, TEXT_LAYER, synthetic_alignment
from text_layer.ass_builder import (ALL_STYLES, S_B, S_B_LOOP, ass_ys, build_ass,
                                    subtract_intervals, wrap_lines)
from text_layer.alignment import align_script_to_words, find_anchor
from text_layer.schema import TextLayerError, parse_text_layer

B_STYLES = {S_B.name, S_B_LOOP.name}

Evento = collections.namedtuple("Evento", "layer start end style role text")


def parse_events(ass_text, role=None):
    """El .ass → [Evento]. `role` filtra por papel (halo / drop / text / ...)."""
    def secs(tc):
        h, m, s = tc.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    out = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue: "):
            continue
        f = line[len("Dialogue: "):].split(",", 9)
        ev = Evento(int(f[0]), secs(f[1]), secs(f[2]), f[3], f[4], f[9])
        if role is None or ev.role == role:
            out.append(ev)
    return out


def sin_tags(texto):
    return re.sub(r"\{[^}]*\}", "", texto)


def build(layer_dict=None, words=None, offset=0.0):
    layer = parse_text_layer(copy.deepcopy(layer_dict or TEXT_LAYER))
    return build_ass(layer, words or synthetic_alignment(), offset=offset)


class TestEstructura(unittest.TestCase):
    def test_el_gancho_genera_los_nueve_rotulos(self):
        ass, report = build()
        self.assertEqual(report["cues"], {"B_LOOP": 2, "C": 3, "E": 1, "E_Q": 3})
        visibles = parse_events(ass, "text")
        # Lineas visibles de cues: E (1 cifra + 1 pregunta) + tres E_Q
        # (1 + 1, 1 + 1, 1 + 2 porque la tercera pregunta ocupa dos lineas)
        # + los tres golpes C (2 + 2 + 1 lineas).
        estilos = collections.Counter(e.style for e in visibles)
        self.assertEqual(estilos["EBIG"], 1)
        self.assertEqual(estilos["ESMALL"], 1)
        self.assertEqual(estilos["EQBIG"], 3)
        self.assertEqual(estilos["EQSMALL"], 4)
        self.assertEqual(estilos["C"], 2)
        self.assertEqual(estilos["CMID"], 3)
        self.assertEqual(len(parse_events(ass, "rule")), 4)
        self.assertEqual(len(parse_events(ass, "count")), 3)
        self.assertGreater(report["b_events"], 0)

    def test_cabecera_y_estilos(self):
        ass, _ = build()
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("PlayResY: 1080", ass)
        self.assertIn("ScaledBorderAndShadow: yes", ass)
        # WrapStyle 2 = libass no reparte lineas: las reparte el builder.
        self.assertIn("WrapStyle: 2", ass)
        # Fontsize de ASS, que no es el px del CSS (ver metrics.ass_fontsize):
        # B 79 px → 109, C 177 → 307, E_Q big 250 → 433.
        self.assertIn("Style: B,Cormorant Garamond Medium,109,", ass)
        self.assertIn("Style: C,Anton,307,", ass)
        self.assertIn("Style: EQBIG,Anton,433,", ass)
        # Ningun estilo lleva borde ni sombra propios: se dibujan como eventos.
        for line in ass.splitlines():
            if line.startswith("Style:"):
                campos = line.split(",")
                self.assertEqual(float(campos[16]), 0.0, f"Outline en {line}")
                self.assertEqual(float(campos[17]), 0.0, f"Shadow en {line}")

    def test_los_tamanos_son_los_del_mock(self):
        from text_layer.ass_builder import S_B, S_C, S_C_MID, S_E_BIG, S_E_SMALL, S_EQ_BIG
        # px de CSS medidos en el mock a 1920x1080.
        self.assertEqual((S_B.size, S_C.size, S_C_MID.size), (79, 177, 211))
        self.assertEqual((S_E_BIG.size, S_E_SMALL.size, S_EQ_BIG.size), (211, 69, 250))

    def test_acento_ambar_inline(self):
        ass, _ = build()
        golpes = [e for e in parse_events(ass, "text") if e.style == "C"]
        texto = " ".join(e.text for e in golpes)
        self.assertIn(r"{\c&H003FACE3&}CON VIDA{\c&H00FFFFFF&}", texto)
        self.assertNotIn(r"{\r}", texto)   # \r borraria el \pos del evento

    def test_la_sombra_no_lleva_el_acento(self):
        ass, _ = build()
        for e in parse_events(ass, "drop") + parse_events(ass, "halo"):
            self.assertNotIn("&H003FACE3&", e.text, "la sombra tiene que ser toda negra")

    def test_rotulo_e_son_tres_capas_y_el_eq_cuatro(self):
        ass, _ = build()
        events = parse_events(ass)
        # El rotulo E de 0:00: cifra + linea ambar + frase, mismo intervalo.
        e01 = [e for e in events if (e.start, e.end) == (0.0, 3.8)
               and e.role in ("text", "rule")]
        self.assertEqual([e.style for e in e01], ["EBIG", "ERULE", "ESMALL"])
        # Y en orden de capa creciente, para que la linea no tape a la cifra.
        self.assertEqual([e.layer for e in e01], sorted(e.layer for e in e01))
        self.assertEqual(len(parse_events(ass, "count")), 3)

    def test_la_linea_ambar_mide_269x5(self):
        ass, _ = build()
        self.assertIn("m 0 0 l 269 0 l 269 5 l 0 5", parse_events(ass, "rule")[0].text)

    def test_offset_del_teaser(self):
        ass, _ = build(offset=13.6)
        self.assertAlmostEqual(min(e.start for e in parse_events(ass)), 13.6, places=1)

    def test_b_loop_usa_cursiva_ambar(self):
        ass, report = build()
        self.assertEqual(report["b_loop_events"], 0)
        # Las dos frases marcadas como B_LOOP caen bajo un cue C, asi que su
        # subtitulo queda suprimido: eso es correcto y es justo lo que dice el
        # encargo (nunca dos capas). Sin esos cues, si tienen que salir.
        sin_c = copy.deepcopy(TEXT_LAYER)
        sin_c["cues"] = [c for c in sin_c["cues"] if c["type"] != "C"]
        ass2, report2 = build(sin_c)
        self.assertGreaterEqual(report2["b_loop_events"], 2)
        loops = [e for e in parse_events(ass2, "text") if e.style == S_B_LOOP.name]
        self.assertTrue(any("encajan" in e.text for e in loops))


class TestNuncaDosCapas(unittest.TestCase):
    def test_b_se_recorta_bajo_los_cues(self):
        ass, report = build()
        events = parse_events(ass)
        b = [e for e in events if e.style in B_STYLES]
        overlays = [e for e in events if e.style not in B_STYLES]
        self.assertGreater(report["b_events_trimmed"], 0)
        for bs, be in [(e.start, e.end) for e in b]:
            for os_, oe in [(e.start, e.end) for e in overlays]:
                self.assertFalse(bs < oe - 1e-6 and os_ < be - 1e-6,
                                 f"B {bs:.2f}-{be:.2f} solapa con cue {os_:.2f}-{oe:.2f}")

    def test_un_cue_en_medio_parte_la_frase_en_dos(self):
        from text_layer.ass_builder import BEvent
        from text_layer.alignment import Word
        ev = BEvent(0.0, 10.0, [Word("x", 0.0, 10.0)])
        out, trimmed = subtract_intervals([ev], [(4.0, 6.0)])
        self.assertEqual(trimmed, 1)
        self.assertEqual([(round(e.start, 2), round(e.end, 2)) for e in out],
                         [(0.0, 4.0), (6.0, 10.0)])

    def test_los_restos_por_debajo_de_un_segundo_se_tiran(self):
        from text_layer.ass_builder import BEvent
        from text_layer.alignment import Word
        ev = BEvent(0.0, 5.0, [Word("x", 0.0, 5.0)])
        out, _ = subtract_intervals([ev], [(0.4, 5.0)])
        self.assertEqual(out, [], "un parpadeo de 0,4 s es peor que nada")


class TestSubtituloB(unittest.TestCase):
    def test_nunca_mas_de_dos_lineas_ni_mas_de_42_caracteres(self):
        ass, _ = build()
        por_evento = collections.defaultdict(list)
        for e in parse_events(ass, "text"):
            if e.style in B_STYLES:
                por_evento[(e.start, e.end)].append(sin_tags(e.text))
        self.assertTrue(por_evento)
        for clave, lineas in por_evento.items():
            self.assertLessEqual(len(lineas), 2, lineas)
            for l in lineas:
                self.assertLessEqual(len(l), 42, l)

    def test_cada_linea_cabe_en_el_ancho_util(self):
        from text_layer.ass_builder import S_B, S_C, S_C_MID, S_EQ_BIG, S_EQ_SMALL
        from text_layer.metrics import text_width
        estilos = {s.name: s for s in
                   __import__("text_layer.ass_builder", fromlist=["x"]).ALL_STYLES}
        ass, _ = build()
        for e in parse_events(ass, "text"):
            st = estilos[e.style]
            w = text_width(sin_tags(e.text), st.font, st.size, st.spacing)
            self.assertLessEqual(w, st.max_width, f"{e.style}: {sin_tags(e.text)!r}")

    def test_una_frase_corta_no_se_parte(self):
        self.assertEqual(wrap_lines("Hasta ahora.", S_B, 2, 42), ["Hasta ahora."])

    def test_prefiere_cortar_en_puntuacion(self):
        texto = "Nadie volvera a verla con vida, dijo la Marina"
        self.assertEqual(wrap_lines(texto, S_B, 2, 42),
                         ["Nadie volvera a verla con vida,", "dijo la Marina"])

    def test_todos_los_eventos_duran_al_menos_un_segundo(self):
        ass, _ = build()
        for e in parse_events(ass):
            if e.style in B_STYLES:
                self.assertGreaterEqual(e.end - e.start, 1.0 - 1e-6)


class TestAlineacion(unittest.TestCase):
    def test_el_guion_manda_sobre_la_transcripcion(self):
        words = synthetic_alignment()
        # Whisper "oye" mal dos palabras y se come una tilde.
        roto = [type(w)(w.word, w.start, w.end) for w in words]
        roto[6].word = "Amalia"
        roto[7].word = "Earheart"
        alineado = align_script_to_words(SCRIPT, roto)
        self.assertEqual(alineado[6].word, "Amelia")
        self.assertEqual(alineado[7].word, "Earhart")
        self.assertEqual(alineado[6].start, roto[6].start)
        self.assertEqual(len(alineado), len(SCRIPT.split()))

    def test_una_palabra_que_whisper_se_comio_recibe_tiempo_interpolado(self):
        words = synthetic_alignment()
        faltan = words[:6] + words[8:]
        alineado = align_script_to_words(SCRIPT, faltan)
        self.assertEqual(len(alineado), len(SCRIPT.split()))
        for a, b in zip(alineado, alineado[1:]):
            self.assertLessEqual(a.start, b.start + 1e-6)

    def test_guion_de_otro_video_falla(self):
        with self.assertRaises(TextLayerError) as ctx:
            align_script_to_words("Stonehenge es un monumento megalitico del sur de Inglaterra "
                                  "levantado en varias fases entre el neolitico y la edad del bronce",
                                  synthetic_alignment())
        self.assertIn("no casan", str(ctx.exception))

    def test_anchor_tolera_la_transcripcion_imperfecta(self):
        words = synthetic_alignment()
        hit = find_anchor("Pero hay tres cosas que no encajan", words)
        self.assertIsNotNone(hit)
        self.assertEqual(" ".join(w.word for w in words[hit[2]:hit[3]]),
                         "Pero hay tres cosas que no encajan.")


class TestValidaciones(unittest.TestCase):
    def _falla(self, mutar, fragmento):
        layer = copy.deepcopy(TEXT_LAYER)
        mutar(layer)
        with self.assertRaises(TextLayerError) as ctx:
            build_ass(parse_text_layer(layer), synthetic_alignment())
        self.assertIn(fragmento, str(ctx.exception).lower())

    def test_golpe_c_demasiado_largo(self):
        def m(l):
            l["cues"][1]["lines"] = ["NADIE VOLVERA JAMAS A VERLA", "CON VIDA NUNCA MAS DE LOS JAMASES"]
        self._falla(m, "maximo 8")

    def test_golpe_c_de_tres_lineas(self):
        def m(l):
            l["cues"][1]["lines"] = ["NADIE", "VOLVERA", "CON VIDA"]
        self._falla(m, "maximo 2")

    def test_acento_que_no_esta_en_las_lineas(self):
        def m(l):
            l["cues"][1]["accent"] = "SIN RASTRO"
        self._falla(m, "no aparece en lines")

    def test_tipo_desconocido(self):
        def m(l):
            l["cues"][1]["type"] = "B"
        self._falla(m, "no valido")

    def test_eq_sin_contador(self):
        def m(l):
            del l["cues"][3]["counter"]
        self._falla(m, "counter")

    def test_rotulo_sin_linea_pequena(self):
        def m(l):
            l["cues"][0]["small"] = ""
        self._falla(m, "small")

    def test_b_loop_con_tiempos(self):
        def m(l):
            l["cues"][7]["start"] = 1.0
            l["cues"][7]["end"] = 3.0
        self._falla(m, "no lleva tiempos")

    def test_cue_c_por_debajo_de_dos_segundos(self):
        def m(l):
            c = l["cues"][1]
            c.pop("anchor_text")
            c["start"], c["end"] = 8.0, 9.4
        self._falla(m, "al menos 2.0s")

    def test_rotulo_e_por_debajo_de_tres_segundos(self):
        def m(l):
            l["cues"][0]["end"] = 2.0
        self._falla(m, "al menos 3.0s")

    def test_tiempos_y_anchor_a_la_vez(self):
        def m(l):
            l["cues"][1]["start"], l["cues"][1]["end"] = 8.0, 11.0
        self._falla(m, "elige uno")

    def test_cues_solapados(self):
        def m(l):
            l["cues"][0]["end"] = 12.0
        self._falla(m, "solapan")

    def test_anchor_que_no_existe_en_la_narracion(self):
        def m(l):
            l["cues"][1]["anchor_text"] = "El radiofaro de Howland nunca respondio"
        self._falla(m, "no aparece en la narracion")

    def test_el_error_de_anchor_sugiere_candidatos(self):
        layer = copy.deepcopy(TEXT_LAYER)
        layer["cues"][1]["anchor_text"] = "El radiofaro de Howland nunca llego a responder"
        with self.assertRaises(TextLayerError) as ctx:
            build_ass(parse_text_layer(layer), synthetic_alignment())
        self.assertIn("lo mas parecido", str(ctx.exception).lower())

    def test_fondo_negro_solo_en_cues_c(self):
        def m(l):
            l["cues"][0]["background"] = "black"
        self._falla(m, "de los cues c")

    def test_fondo_negro_fuerza_el_titular_centrado(self):
        layer = parse_text_layer(copy.deepcopy(TEXT_LAYER))
        c02 = [c for c in layer.cues if c.id == "c02"][0]
        self.assertEqual(c02.placement, "mid")

    def test_ids_repetidos(self):
        def m(l):
            l["cues"][1]["id"] = "e01"
        self._falla(m, "repetido")

    def test_version_desconocida(self):
        def m(l):
            l["version"] = 2
        self._falla(m, "solo entiende la 1")

    def test_mas_de_dos_lineas_de_b(self):
        def m(l):
            l["b_style"]["max_lines"] = 3
        self._falla(m, "2 como maximo")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNoSePierdeNiUnaPalabra(unittest.TestCase):
    """El subtitulo B tiene que decir lo que dice la narracion, entera.

    Se perdian dos cosas: los rabos de frase de menos de un segundo (se
    descartaban) y las frases que no caben en dos lineas (el reparto de
    emergencia truncaba). Las dos salieron al preparar el video 7.
    """

    GUION = ("Hacia el año 1200 antes de Cristo, las cartas dejan de llegar. "
             "Egipto, el Imperio hitita, la Grecia micénica, Chipre y el Levante "
             "mantenían rutas comerciales, embajadas, tratados matrimoniales y "
             "correspondencia diplomática continua. "
             "Lo que ocurrió entre 1200 y 1150 antes de Cristo sigue sin tener "
             "una causa única confirmada. "
             "Todo eso lo vas a descubrir en los próximos minutos. Empezamos.")

    def _construir(self, cues=()):
        from tests.gancho_amelia import synthetic_alignment
        words = synthetic_alignment(self.GUION)
        layer = parse_text_layer({"version": 1,
                                  "cues": list(cues) or [
                                      {"id": "l1", "type": "B_LOOP",
                                       "anchor_text": "Empezamos."}],
                                  "b_style": {"max_lines": 2, "max_chars_per_line": 42}})
        return build_ass(layer, words), words

    def test_todas_las_palabras_del_guion_salen_en_pantalla(self):
        (ass, report), words = self._construir()
        from text_layer.schema import normalize
        visto = " ".join(sin_tags(e.text) for e in parse_events(ass, "text")
                         if e.style in B_STYLES)
        faltan = (collections.Counter(normalize(self.GUION).split())
                  - collections.Counter(normalize(visto).split()))
        self.assertEqual(sum(faltan.values()), 0,
                         f"el subtitulo se come palabras: {dict(faltan)}")
        self.assertEqual(report["b_events_hard_split"], [])

    def test_una_frase_larga_no_se_trunca(self):
        (ass, _), _ = self._construir()
        from text_layer.schema import normalize
        visto = normalize(" ".join(sin_tags(e.text) for e in parse_events(ass, "text")))
        for trozo in ["tratados matrimoniales", "correspondencia diplomatica continua",
                      "una causa unica confirmada", "empezamos"]:
            self.assertIn(trozo, visto, trozo)

    def test_bajo_un_cue_si_se_suprime_pero_no_en_otro_sitio(self):
        (ass, _), words = self._construir(cues=[
            {"id": "c1", "type": "C", "anchor_text": "Empezamos.",
             "lines": ["EMPEZAMOS"], "accent": "EMPEZAMOS"}])
        texto = [e for e in parse_events(ass, "text")]
        golpes = [e for e in texto if e.style == "C"]
        self.assertEqual(len(golpes), 1)
        # Y ninguna capa B pisa al golpe.
        for b in [e for e in texto if e.style in B_STYLES]:
            self.assertFalse(b.start < golpes[0].end - 1e-6
                             and golpes[0].start < b.end - 1e-6)


class TestLineasQueNoSePisan(unittest.TestCase):
    """En Anton una mayuscula acentuada mide 1,101 em, mas que el cuadratin.

    Con el `line-height: 1` que pide el mock para el estilo C, un "DÉCADAS" en
    segunda linea mete la tilde por encima de la linea base de la primera y se
    lee como una coma. El mock no cubre el caso porque su unico golpe C de
    ejemplo lleva el acento en la linea de arriba.
    """

    def _paso(self, style, lineas, anchor_y, anchor):
        ys = ass_ys(style, lineas, anchor_y, anchor)
        return ys[1] - ys[0]

    def test_el_golpe_c_del_mock_conserva_su_interlineado(self):
        from text_layer.ass_builder import C_BOTTOM, S_C
        # 177 px = line-height 1 x 177, medido en el navegador (pitch 176,7).
        self.assertEqual(self._paso(S_C, ["NADIE VOLVERÁ", "A VERLA CON VIDA"],
                                    C_BOTTOM, "bottom"), 177)

    def test_una_mayuscula_acentuada_abajo_separa_las_lineas(self):
        from text_layer.ass_builder import MID_CENTER, S_C_MID
        sin = self._paso(S_C_MID, ["NO EN SIGLOS", "EN DECADAS"], MID_CENTER, "center")
        con = self._paso(S_C_MID, ["NO EN SIGLOS", "EN DÉCADAS"], MID_CENTER, "center")
        self.assertEqual(sin, 211, "sin tilde tiene que quedarse en el del mock")
        self.assertGreater(con, sin, "con tilde tiene que separarse")
        self.assertGreaterEqual(con, 232, "la Ê de Anton mide 1,101 em = 232 px a 211")

    def test_el_subtitulo_b_no_se_toca(self):
        from text_layer.ass_builder import B_BOTTOM, S_B
        # line-height 1,28 x 79 = 101 px, medido en el navegador (100,8).
        self.assertEqual(self._paso(S_B, ["La historia oficial dice que se",
                                          "quedó sin combustible y cayó al mar."],
                                    B_BOTTOM, "bottom"), 101)

    def test_el_bloque_sigue_anclado_donde_estaba(self):
        from text_layer.ass_builder import MID_CENTER, S_C_MID
        a = ass_ys(S_C_MID, ["NO EN SIGLOS", "EN DECADAS"], MID_CENTER, "center")
        b = ass_ys(S_C_MID, ["NO EN SIGLOS", "EN DÉCADAS"], MID_CENTER, "center")
        # crece hacia los dos lados por igual: el centro no se mueve
        self.assertAlmostEqual((a[0] + a[1]) / 2, (b[0] + b[1]) / 2, delta=1)


class TestAlineacionLarga(unittest.TestCase):
    """La alineacion tiene que aguantar un guion entero, no solo un gancho.

    El render del video 7 se paro con "solo 599 de 2282 palabras coinciden":
    un `difflib` de una sola pasada sobre 2.300 tokens con una transcripcion
    ruidosa alinea muy mal. Se reescribio por ventanas moviles.
    """

    def _guion_largo(self, n=60):
        # Texto variado y con frases repetidas a proposito ("antes de Cristo"
        # sale muchas veces en los guiones reales de EP), para que la ventana
        # tenga que apoyarse en la posicion y no solo en la unicidad.
        base = ("En el año {i} antes de Cristo la ciudad de {c} deja de aparecer "
                "en los archivos. Nadie sabe por qué. Los arqueólogos encuentran "
                "ceniza pero no encuentran cuerpos, y eso complica la explicación "
                "más sencilla. ")
        ciudades = ["Ugarit", "Hattusa", "Micenas", "Pilos", "Enkomi", "Menfis"]
        return "".join(base.format(i=1200 - k * 7, c=ciudades[k % len(ciudades)])
                       for k in range(n))

    def _transcripcion(self, guion, error_cada=6, wps=2.05):
        """Simula un ASR que se equivoca cada N palabras y parte alguna."""
        from text_layer.alignment import Word
        salida, t = [], 0.0
        for k, tok in enumerate(guion.split()):
            texto = tok if k % error_cada else tok[:-1] + "z" if len(tok) > 3 else tok
            d = 1.0 / wps
            salida.append(Word(texto, round(t, 3), round(t + d * 0.9, 3)))
            t += d
        return salida

    def test_un_guion_de_2000_palabras_se_alinea_entero(self):
        guion = self._guion_largo()
        oido = self._transcripcion(guion)
        self.assertGreater(len(guion.split()), 1000)
        alineado = align_script_to_words(guion, oido)
        self.assertEqual(len(alineado), len(guion.split()))
        # Y el texto que sale es el del GUION, no el del ASR.
        self.assertEqual([w.word for w in alineado], guion.split())

    def test_los_tiempos_van_siempre_hacia_delante(self):
        guion = self._guion_largo()
        alineado = align_script_to_words(guion, self._transcripcion(guion))
        for x, y in zip(alineado, alineado[1:]):
            self.assertGreaterEqual(y.start, x.start - 1e-6)

    def test_las_frases_caen_donde_toca_pese_a_repetirse(self):
        from text_layer.alignment import find_anchor
        guion = self._guion_largo()
        oido = self._transcripcion(guion)
        alineado = align_script_to_words(guion, oido)
        total = oido[-1].end
        # "antes de Cristo" sale en las 60 frases; la ULTIMA tiene que caer al
        # final del audio, no emparejarse con una repeticion anterior.
        ciudades = ["Ugarit", "Hattusa", "Micenas", "Pilos", "Enkomi", "Menfis"]
        k = 59
        ultima = (f"En el año {1200 - k * 7} antes de Cristo la ciudad de "
                  f"{ciudades[k % len(ciudades)]} deja de aparecer")
        self.assertIn(ultima, guion, "la frase de control no esta en el guion")
        hit = find_anchor(ultima, alineado)
        self.assertIsNotNone(hit)
        self.assertGreater(hit[0], total * 0.85,
                           "una frase del final se ha emparejado con una repeticion anterior")

    def test_un_guion_de_otro_video_sigue_fallando(self):
        guion = self._guion_largo()
        otro = ("Amelia Earhart despega de Lae en Nueva Guinea y nadie vuelve a "
                "verla con vida jamás. " * 40)
        with self.assertRaises(TextLayerError) as ctx:
            align_script_to_words(otro, self._transcripcion(guion))
        self.assertIn("no casan", str(ctx.exception))
