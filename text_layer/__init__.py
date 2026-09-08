"""Capa de texto de ENIGMAS DEL PASADO.

Tres estilos decididos desde el guion (B documental, C golpe, E rotulo de dato)
quemados con FFmpeg + ASS/libass. Sustituye al subtitulo unico de Liberation
Sans que el renderer generaba con Whisper, y elimina el paso manual de CapCut.

La verdad visual son los dos mocks de `docs/referencia-subtitulos/` del repo EP.
"""

from .schema import TextLayer, TextLayerError, parse_text_layer
from .alignment import (Word, align_script_to_words, words_from_elevenlabs,
                        words_from_payload, words_from_whisper)
from .ass_builder import build_ass
from .burn import build_video_filters

__all__ = [
    "TextLayer",
    "TextLayerError",
    "parse_text_layer",
    "Word",
    "align_script_to_words",
    "words_from_elevenlabs",
    "words_from_payload",
    "words_from_whisper",
    "build_ass",
    "build_video_filters",
]
