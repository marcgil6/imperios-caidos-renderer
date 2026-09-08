"""Guion del gancho de Amelia Earhart y su capa de texto.

El guion es el que aparece en `docs/referencia-subtitulos/gancho-ep.html`
(seccion "Guion del gancho"), y la capa de texto reproduce los nueve
fotogramas del storyboard. Se usa tanto en los tests unitarios como en el
test visual.
"""

SCRIPT = (
    "El 2 de julio de 1937, Amelia Earhart despega de Lae, en Nueva Guinea. "
    "Nadie volverá a verla con vida. "
    "La historia oficial dice que se quedó sin combustible y cayó al mar. "
    "Pero hay tres cosas que no encajan. "
    "La Marina de Estados Unidos abandona la búsqueda a los dieciséis días. "
    "Durante cinco noches, alguien sigue transmitiendo desde la radio de su avión. "
    "Y en 1940, en una isla desierta, aparecen unos huesos que después desaparecen. "
    "Esta es la historia que nadie quiso investigar. "
    "Hasta ahora."
)

# Los nueve fotogramas: 0:00 E · 0:04 B · 0:08 C · 0:12 B · 0:16 C mid negro ·
# 0:19 E_Q 1/3 · 0:24 E_Q 2/3 · 0:29 E_Q 3/3 · 0:35 B + C mid "HASTA AHORA".
TEXT_LAYER = {
    "version": 1,
    "cues": [
        {"id": "e01", "type": "E", "start": 0.0, "end": 3.8,
         "big": "2 DE JULIO", "big_accent": "DE 1937", "small": "Lae, Nueva Guinea"},

        {"id": "c01", "type": "C", "anchor_text": "Nadie volverá a verla con vida.",
         "lines": ["NADIE VOLVERÁ", "A VERLA CON VIDA"], "accent": "CON VIDA"},

        {"id": "c02", "type": "C", "anchor_text": "Pero hay tres cosas que no encajan.",
         "placement": "mid", "background": "black",
         "lines": ["PERO HAY TRES COSAS", "QUE NO ENCAJAN"], "accent": "TRES COSAS"},

        {"id": "q01", "type": "E_Q", "counter": "1 / 3",
         "anchor_text": "La Marina de Estados Unidos abandona la búsqueda a los dieciséis días.",
         "big": "DÍAS", "big_accent": "16", "accent_first": True,
         "small": "¿Por qué la Marina dejó de buscarla?"},

        {"id": "q02", "type": "E_Q", "counter": "2 / 3",
         "anchor_text": "Durante cinco noches, alguien sigue transmitiendo desde la radio de su avión.",
         "big": "NOCHES", "big_accent": "5", "accent_first": True,
         "small": "¿Quién seguía transmitiendo desde su radio?"},

        {"id": "q03", "type": "E_Q", "counter": "3 / 3",
         "anchor_text": "Y en 1940, en una isla desierta, aparecen unos huesos que después desaparecen.",
         "big": "", "big_accent": "1940",
         "small": "¿Qué huesos aparecieron… y por qué desaparecieron?"},

        {"id": "c03", "type": "C", "anchor_text": "Hasta ahora.",
         "placement": "mid", "lines": ["HASTA AHORA"], "accent": "HASTA AHORA"},

        {"id": "b_open_loop_01", "type": "B_LOOP",
         "anchor_text": "Pero hay tres cosas que no encajan."},
        {"id": "b_open_loop_02", "type": "B_LOOP", "anchor_text": "Hasta ahora."},
    ],
    "b_style": {"max_lines": 2, "max_chars_per_line": 42},
}


def synthetic_alignment(script=SCRIPT, chars_per_sec=13.5, start=0.35, pause=0.45):
    """Alineacion sintetica calibrada contra los tiempos del storyboard.

    No pretende ser realista frase a frase: solo colocar las nueve frases del
    gancho cerca de los 0:00 / 0:04 / 0:08 / 0:12 / 0:16 / 0:19 / 0:24 / 0:29 /
    0:35 del mock, para que los tests trabajen sobre tiempos reconocibles.
    El test visual usa TTS + Whisper de verdad, no esto.
    """
    from text_layer.alignment import Word

    words, t = [], start
    for token in script.split():
        dur = max(len(token) + 1, 3) / chars_per_sec
        words.append(Word(token, round(t, 3), round(t + dur, 3)))
        t += dur
        stripped = token.rstrip('"\u201d\u00bb)')
        if stripped.endswith((".", "!", "?", "\u2026")):
            t += pause
        elif stripped.endswith((",", ";", ":")):
            t += pause / 3
    return words
