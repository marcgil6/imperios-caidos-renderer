# Biblioteca musical del canal

**Estado (2026-08-28): LLENA — 24 pistas, 3 por mood.** Todas salen de la
Biblioteca de audio de YouTube con el filtro **"No requiere atribucion"**
(licencia YouTube, sin Content ID), descargadas el 28/08/2026 y
recodificadas a MP3 192 kbps 44,1 kHz estereo. `library.json` lleva titulo,
artista, duracion y el LUFS ya medido de cada una (ahorra una medicion por
render). Las tres pistas de Scott Buckley se retiraron del repo en el mismo
commit: su Smart Content ID reclamaba los videos aunque el credito
estuviera puesto.

Todo archivo de audio que se deje en esta carpeta entra automáticamente en el
sistema. **No hay que tocar código ni desplegar nada más que la imagen.**

## Convención de nombres (es el manifiesto)

    <mood>_<intensidad>_<NN>.mp3

* `mood` — uno de: `mystery`, `tension`, `ancient`, `dark`, `discovery`,
  `emotional`, `atmospheric`, `neutral`
* `intensidad` — `low`, `medium` o `high` (si falta se asume `medium`)
* `NN` — número correlativo para no repetir nombre

Ejemplos: `mystery_low_01.mp3`, `tension_high_02.mp3`, `ancient_low_03.mp3`

Un archivo cuyo nombre no contenga un mood válido **se ignora** (así no se
cuela un efecto de sonido en la rotación por error).

## Cuántas pistas

Objetivo: **3 por mood** (24 en total). Con 3 por mood y el bloqueo de
repetición, hacen falta muchos vídeos antes de que una pista vuelva a sonar
en el mismo contexto. Con 1 sola pista de un mood el sistema sigue
funcionando: repite, pero no falla.

Duración recomendada: 3-6 minutos. Da igual que sean más cortas que el vídeo,
el motor las recicla con crossfade de 4 s (nunca corte seco).

Formato recomendado: MP3 192 kbps (calidad de sobra para una cama a -32 LUFS
y mantiene el peso de la imagen Docker razonable).

## De dónde deben salir

Requisito del canal: **cero reclamaciones de Content ID**. Eso descarta
cualquier fuente que gestione sus derechos vía Content ID aunque la licencia
permita el uso (el caso de las pistas heredadas de Scott Buckley).

Fuentes válidas por orden de preferencia:

1. **YouTube Audio Library** (studio.youtube.com → Audio Library), con el
   filtro **"Attribution not required"**. YouTube garantiza que su propia
   biblioteca no genera reclamaciones de Content ID. Coste 0.
2. **Música generada por IA** con licencia comercial explícita (ElevenLabs
   Music, Suno API…). Nadie más tiene esas pistas, así que no existe huella
   en Content ID. Se genera una vez y se reutiliza para siempre.

Si una pista requiere atribución, hay que declararla en `library.json` (ver
abajo) y el render la devolverá en `music_attribution` para que EP-07 la
escriba en la descripción automáticamente.

## library.json (opcional)

Solo hace falta para metadatos: título, fuente, atribución obligatoria o un
LUFS ya medido (ahorra una medición por render).

```json
{
  "version": 1,
  "tracks": [
    {
      "file": "mystery_low_01.mp3",
      "title": "Nombre de la pista",
      "mood": "mystery",
      "intensity": "low",
      "source": "YouTube Audio Library",
      "attribution": null,
      "lufs": -14.2
    }
  ]
}
```

`attribution: null` = no requiere crédito (el caso deseable).
