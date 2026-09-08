"""Hornea las caras estaticas de Cormorant Garamond que usa la capa de texto EP.

Dos motivos para no usar la fuente variable de Google Fonts tal cual:

1. libass NO interpola ejes variables: carga la instancia por defecto (wght 400),
   asi que un `Fontname: Cormorant Garamond` sobre el .ttf variable daria Regular,
   no el Medium 500 del mock. Seria justo el "fallback silencioso" que el
   encargo prohibe.
2. Cuando varias caras comparten familia, libass elige por los flags Bold/Italic
   con un sistema de puntuacion. Para que la eleccion sea EXACTA y no dependa de
   ese scoring, a cada cara se le da un nombre de familia propio y el ASS la pide
   por ese nombre con Bold=0,Italic=0.
"""
from fontTools import ttLib
from fontTools.varLib import instancer

SRC_ROMAN = "fonts/CormorantGaramond[wght].ttf"
SRC_ITALIC = "fonts/CormorantGaramond-Italic[wght].ttf"

# (fuente, wght, fichero de salida, familia que pedira el ASS, italico)
TARGETS = [
    (SRC_ROMAN,  500, "CormorantGaramond-Medium.ttf",       "Cormorant Garamond Medium",        False),
    (SRC_ITALIC, 500, "CormorantGaramond-MediumItalic.ttf", "Cormorant Garamond Medium Italic", True),
    (SRC_ROMAN,  600, "CormorantGaramond-SemiBold.ttf",     "Cormorant Garamond SemiBold",      False),
]


def set_name(font, name_id, value):
    font["name"].setName(value, name_id, 3, 1, 0x409)   # Windows / Unicode BMP / en-US
    font["name"].setName(value, name_id, 1, 0, 0)       # Mac / Roman / English


for src, wght, out, family, italic in TARGETS:
    font = ttLib.TTFont(src)
    instancer.instantiateVariableFont(font, {"wght": wght}, inplace=True, updateFontNames=True)
    set_name(font, 1, family)                    # Family
    set_name(font, 2, "Regular")                 # Subfamily
    set_name(font, 4, family)                    # Full name
    set_name(font, 6, family.replace(" ", ""))   # PostScript
    set_name(font, 16, family)                   # Typographic family
    set_name(font, 17, "Regular")                # Typographic subfamily
    font["OS/2"].usWeightClass = wght
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0b1100001) | (0b1 if italic else 0b1000000)
    font["head"].macStyle = 0b10 if italic else 0
    font.save(f"fonts/{out}")
    print(f"{out:38} family={family!r} wght={wght} italic={italic}")
