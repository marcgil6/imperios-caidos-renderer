# Fuentes de la capa de texto de ENIGMAS DEL PASADO

| Fichero | Familia que pide el ASS | Usos |
|---|---|---|
| `Anton-Regular.ttf` | `Anton` | golpes C, cifra de los rótulos E/E_Q, miniaturas EP-08 |
| `CormorantGaramond-Medium.ttf` | `Cormorant Garamond Medium` | contador «1 / 3» |
| `CormorantGaramond-MediumItalic.ttf` | `Cormorant Garamond Medium Italic` | subtítulo B_LOOP, frase pequeña de los rótulos |
| `CormorantGaramond-SemiBold.ttf` | `Cormorant Garamond SemiBold` | ninguno hoy; está porque el mock lo declara |

El subtítulo B usa `Cormorant Garamond Medium`. Todas son de Google Fonts con
licencia OFL (`OFL-CormorantGaramond.txt`).

## Por qué cada cara tiene un nombre de familia propio

Lo normal sería que las tres Cormorant compartieran familia y se distinguieran
por los flags `Bold`/`Italic` del ASS. No se hace así por dos motivos:

1. **libass no interpola fuentes variables.** Google Fonts ya solo publica
   Cormorant Garamond como variable (`CormorantGaramond[wght].ttf`), y libass
   cargaría su instancia por defecto, que es Regular 400, no el Medium 500 del
   mock. Las tres caras se hornean como estáticas con `fontTools`.
2. **La elección por flags es una puntuación, no una coincidencia exacta.**
   Con familias distintas, `Fontname` casa exactamente y no puede caer en una
   fuente de sustitución sin avisar — que es el fallo silencioso más caro aquí,
   porque el vídeo sale con la tipografía equivocada y no lo detecta nadie
   hasta verlo.

El `Dockerfile` rompe el build si `fc-list` no resuelve las cuatro familias, y
`/health` y `/test-subs` las reportan en caliente.

## Regenerar las Cormorant

```bash
curl -L -o 'CormorantGaramond[wght].ttf' \
  'https://github.com/google/fonts/raw/main/ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf'
curl -L -o 'CormorantGaramond-Italic[wght].ttf' \
  'https://github.com/google/fonts/raw/main/ofl/cormorantgaramond/CormorantGaramond-Italic%5Bwght%5D.ttf'
python scripts/instance_fonts.py      # instancia wght 500 / 500 italic / 600
```
