#!/usr/bin/env python3
"""Guion de EP → `text_layer.json`, con Claude.

    python scripts/build_text_layer.py guion.md -o text_layer.json

Lee el guion, pide a Claude la capa de texto siguiendo las reglas editoriales
del canal y VALIDA la respuesta contra el esquema antes de escribir nada. Si
Claude devuelve algo que no cumple, falla aqui y no en mitad de un render de
veinte minutos.

Coste: una llamada a Sonnet por video, < 0,05 EUR. Se avisa por pantalla ANTES
de llamar, como exige el protocolo del canal.

Este script NO toca n8n ni Airtable: escribe un fichero y ya.

Requisitos (solo en local, NO van en la imagen del renderer):
    pip install anthropic
    export ANTHROPIC_API_KEY=...
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_layer.schema import TextLayerError, parse_text_layer

MODEL = "claude-sonnet-5"
MAX_TOKENS = 8000

# Precios de Sonnet por millon de tokens (USD), solo para estimar el aviso.
PRICE_IN, PRICE_OUT = 3.0, 15.0
USD_EUR = 0.92

SYSTEM = """Eres el director de arte de ENIGMAS DEL PASADO, un canal de YouTube de \
divulgación de misterios históricos. Tu trabajo es decidir la CAPA DE TEXTO en \
pantalla de un vídeo a partir de su guion, y devolverla como JSON.

Hay tres estilos, y solo tres:

- **C · golpe**: 2-6 palabras en mayúsculas, enormes, con una palabra clave en \
ámbar. Es un TITULAR, no una transcripción: resume, no copia.
- **E · rótulo de dato**: una cifra o fecha grande arriba, una línea ámbar y una \
frase corta debajo. Para datos duros: fechas, cifras, nombres, lugares.
- **E_Q · rótulo de incógnita**: igual que E pero con contador "1 / 3". \
EXCLUSIVO de las tres incógnitas del gancho.

El subtítulo normal (estilo B) NO se declara: se genera solo a partir de la \
narración. Lo único que se marca de B son los `B_LOOP`: las frases que van en \
cursiva ámbar porque abren un bucle o giran el argumento (los "Pero...", los \
"Por lo tanto...", los open loops).

REGLAS EDITORIALES, en orden de importancia:

1. GANCHO (0-40 s): exactamente 1 rótulo E de fecha/lugar al principio, 2-4 \
golpes C, y exactamente 3 cues E_Q con contador "1 / 3", "2 / 3", "3 / 3". \
Cada E_Q lleva una cifra o palabra sola en `big_accent` y la pregunta completa \
en `small`. Las tres incógnitas van en escalera: la tercera es la más intrigante.
2. El conector "PERO" del gancho es 1 golpe C con `"placement": "mid"` y \
`"background": "black"`: corte a negro con titular centrado.
3. CUERPO: un rehook cada ~3 minutos = 1-2 golpes C como máximo. Los datos duros \
(fechas, cifras, nombres) = rótulo E, MÁXIMO UNO POR ESCENA.
4. ÚLTIMO MINUTO (la llamada a la acción): solo B. Ni un cue.
5. Los open loops y los conectores PERO / POR LO TANTO = `B_LOOP`.
6. NUNCA dos cues solapados: entre el final de uno y el principio del siguiente \
tiene que haber narración.

FORMATO. Devuelve SOLO el JSON, sin explicaciones y sin ```:

{"version": 1,
 "cues": [
  {"id": "e01", "type": "E", "anchor_text": "<frase LITERAL del guion>",
   "big": "2 DE JULIO", "big_accent": "DE 1937", "small": "Lae, Nueva Guinea"},
  {"id": "c01", "type": "C", "anchor_text": "<frase LITERAL del guion>",
   "lines": ["NADIE VOLVERÁ", "A VERLA CON VIDA"], "accent": "CON VIDA"},
  {"id": "c02", "type": "C", "anchor_text": "<frase LITERAL>",
   "placement": "mid", "background": "black",
   "lines": ["PERO HAY TRES COSAS", "QUE NO ENCAJAN"], "accent": "TRES COSAS"},
  {"id": "q01", "type": "E_Q", "counter": "1 / 3", "anchor_text": "<frase LITERAL>",
   "big": "DÍAS", "big_accent": "16", "accent_first": true,
   "small": "¿Por qué la Marina dejó de buscarla?"},
  {"id": "loop01", "type": "B_LOOP", "anchor_text": "<frase LITERAL del guion>"}
 ],
 "b_style": {"max_lines": 2, "max_chars_per_line": 42}}

RESTRICCIONES DURAS (si las incumples, el JSON se rechaza):
- `anchor_text` tiene que ser una frase COPIADA LITERALMENTE del guion, con sus \
tildes y su puntuación. Es lo que ancla el cue en el tiempo. No la reescribas.
- Un golpe C: máximo 2 líneas y 6 palabras en total. `accent` tiene que aparecer \
tal cual dentro de `lines`, y entero en UNA sola línea.
- Todo E y E_Q necesita `small`. Todo E_Q necesita `counter`.
- `background: "black"` solo en cues C.
- Los `id` son únicos.
- Las líneas de C van en MAYÚSCULAS."""


def _clean_json(text):
    """Quita los backticks que Claude pone a veces y se queda con el objeto."""
    text = re.sub(r"^\s*```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"la respuesta no contiene ningún objeto JSON:\n{text[:400]}")
    return json.loads(text[start:end + 1])


def build(script_text, model=MODEL, api_key=None):
    import anthropic

    words = len(script_text.split())
    est_in = words * 1.6 + 1200          # tokens del guion + el system
    est_out = 2500
    coste = (est_in / 1e6 * PRICE_IN + est_out / 1e6 * PRICE_OUT) * USD_EUR
    print(f"[coste] 1 llamada a {model}: ~{est_in:.0f} tokens de entrada + "
          f"~{est_out} de salida ≈ {coste:.3f} EUR", file=sys.stderr)

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Guion del vídeo ({words} palabras):\n\n{script_text}"}],
    )
    usage = resp.usage
    real = (usage.input_tokens / 1e6 * PRICE_IN
            + usage.output_tokens / 1e6 * PRICE_OUT) * USD_EUR
    print(f"[coste] real: {usage.input_tokens} in + {usage.output_tokens} out "
          f"= {real:.3f} EUR", file=sys.stderr)

    return _clean_json("".join(b.text for b in resp.content if b.type == "text"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("guion", help="fichero .txt o .md con el guion")
    ap.add_argument("-o", "--out", default="text_layer.json")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="solo estima el coste, no llama a la API")
    args = ap.parse_args()

    with open(args.guion, encoding="utf-8") as f:
        script_text = f.read()

    if args.dry_run:
        words = len(script_text.split())
        est = ((words * 1.6 + 1200) / 1e6 * PRICE_IN + 2500 / 1e6 * PRICE_OUT) * USD_EUR
        print(f"{words} palabras → coste estimado {est:.3f} EUR (no se ha llamado a la API)")
        return 0

    raw = build(script_text, args.model)

    # Validar ANTES de escribir: mas vale fallar aqui que en el render.
    try:
        layer = parse_text_layer(raw)
    except TextLayerError as e:
        print(f"\n[ERROR] Claude devolvió una capa inválida: {e}", file=sys.stderr)
        bad = args.out + ".rechazado"
        with open(bad, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1)
        print(f"        El JSON crudo queda en {bad} para que lo mires.", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)

    cuenta = {t: sum(1 for c in layer.cues if c.type == t)
              for t in ("C", "E", "E_Q", "B_LOOP")}
    print(f"\n{args.out}: {len(layer.cues)} cues {cuenta}")
    if cuenta["E_Q"] != 3:
        print(f"[aviso] el gancho debería llevar 3 incógnitas E_Q y lleva "
              f"{cuenta['E_Q']}.", file=sys.stderr)
    print("Los tiempos se resuelven en el render, contra la alineación del audio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
