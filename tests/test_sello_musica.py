"""Tests del candado de licencia de la musica (sello_musica).

El 23/09/2026 se publico el video 4 con un MP4 renderizado el 05/08, cuando la
cama eran pistas de Scott Buckley con Smart Content ID, y YouTube lo reclamo. La
biblioteca llevaba limpia desde el 31/08: lo que fallo es que nada impedia subir
un fichero viejo. Estos tests fijan los dos candados: que solo suene musica
certificada y que no se pueda subir un MP4 sin sello.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_mix
import sello_musica
import youtube_upload

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRERIA = os.path.join(RAIZ, "music", "library")
HAY_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _mp4(path, metadata=()):
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", "1", "-c:v", "libx264", "-c:a", "aac", "-shortest",
         "-movflags", "+faststart", *metadata, path],
        check=True)


class TestBibliotecaReal(unittest.TestCase):
    def test_las_24_pistas_estan_certificadas(self):
        # Si alguien toca un mp3 o el manifiesto sin actualizar el otro, el
        # render se quedaria sin esa pista: mejor enterarse aqui.
        manifiesto = sello_musica.cargar_manifiesto(LIBRERIA)
        mp3 = [f for f in os.listdir(LIBRERIA) if f.endswith(".mp3")]
        self.assertEqual(len(mp3), 24)
        for f in mp3:
            self.assertIsNone(
                sello_musica.motivo_rechazo(os.path.join(LIBRERIA, f), manifiesto), f)

    def test_ninguna_pista_exige_atribucion(self):
        for t in sello_musica.cargar_manifiesto(LIBRERIA).values():
            self.assertIsNone(t["attribution"], t["file"])
            self.assertEqual(t["source"], sello_musica.ORIGEN_OK, t["file"])


class TestCandadoEntrada(unittest.TestCase):
    """Una pista que no pase por el manifiesto no suena nunca."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        origen = os.path.join(LIBRERIA, "mystery_low_01.mp3")
        shutil.copy(origen, os.path.join(self.dir, "mystery_low_01.mp3"))
        manifiesto = sello_musica.cargar_manifiesto(LIBRERIA)
        self.entrada = dict(manifiesto["mystery_low_01.mp3"])

    def _escribir(self, *entradas):
        with open(os.path.join(self.dir, "library.json"), "w") as fh:
            json.dump({"tracks": list(entradas)}, fh)

    def test_pista_certificada_entra(self):
        self._escribir(self.entrada)
        self.assertEqual([t["id"] for t in audio_mix.load_library(self.dir)],
                         ["mystery_low_01"])

    def test_mp3_copiado_sin_manifiesto_no_entra(self):
        # El caso Scott Buckley: un mp3 con nombre de mood valido que nadie
        # ha certificado.
        self._escribir(self.entrada)
        shutil.copy(os.path.join(self.dir, "mystery_low_01.mp3"),
                    os.path.join(self.dir, "tension_medium_09.mp3"))
        ids = [t["id"] for t in audio_mix.load_library(self.dir)]
        self.assertEqual(ids, ["mystery_low_01"])

    def test_licencia_con_atribucion_no_entra(self):
        self.entrada["attribution"] = "Music by Scott Buckley - CC BY 4.0"
        self._escribir(self.entrada)
        self.assertEqual(audio_mix.load_library(self.dir), [])

    def test_otro_origen_no_entra(self):
        self.entrada["source"] = "scottbuckley.com.au"
        self._escribir(self.entrada)
        self.assertEqual(audio_mix.load_library(self.dir), [])

    def test_fichero_cambiado_no_entra(self):
        # Mismo nombre, otro contenido: el sha256 lo caza.
        self._escribir(self.entrada)
        with open(os.path.join(self.dir, "mystery_low_01.mp3"), "ab") as fh:
            fh.write(b"otra cancion")
        self.assertEqual(audio_mix.load_library(self.dir), [])

    def test_sin_manifiesto_no_entra_nada(self):
        self.assertEqual(audio_mix.load_library(self.dir), [])

    def test_reserva_heredada_tambien_pasa_el_candado(self):
        self._escribir()
        falsa = os.path.join(self.dir, "uprising.mp3")
        shutil.copy(os.path.join(self.dir, "mystery_low_01.mp3"), falsa)
        self.assertEqual(audio_mix.load_library(
            self.dir, [{"id": "uprising", "path": falsa, "mood": "tension"}]), [])


@unittest.skipUnless(HAY_FFMPEG, "hace falta ffmpeg")
class TestCandadoSalida(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_sello_ida_y_vuelta(self):
        p = os.path.join(self.dir, "ok.mp4")
        _mp4(p, sello_musica.args_sello(sello_musica.MUSICA_LIBRE,
                                        ["mystery_low_01", "dark_low_01"], "b1"))
        sello = sello_musica.exigir_sello(p)
        self.assertEqual(sello["ep-music"], "yt-audio-library")
        self.assertEqual(sello["tracks"], "mystery_low_01,dark_low_01")

    def test_narracion_sola_vale(self):
        p = os.path.join(self.dir, "voz.mp4")
        _mp4(p, sello_musica.args_sello(sello_musica.SIN_MUSICA))
        self.assertEqual(sello_musica.exigir_sello(p)["ep-music"], "none")

    def test_render_viejo_sin_sello_se_rechaza(self):
        p = os.path.join(self.dir, "VID4_viejo.mp4")
        _mp4(p)
        with self.assertRaises(sello_musica.MusicaNoCertificada):
            sello_musica.exigir_sello(p)

    def test_sello_inventado_se_rechaza(self):
        p = os.path.join(self.dir, "raro.mp4")
        _mp4(p, ["-metadata", "comment=ep-music=scott-buckley"])
        with self.assertRaises(sello_musica.MusicaNoCertificada):
            sello_musica.exigir_sello(p)

    def test_subida_bloqueada_antes_de_gastar_cuota(self):
        # Lo importante: ni se llega a pedir el servicio de YouTube.
        p = os.path.join(self.dir, "VID4_viejo.mp4")
        _mp4(p)
        with mock.patch.object(youtube_upload, "_service") as servicio:
            with self.assertRaises(sello_musica.MusicaNoCertificada):
                youtube_upload.upload(p, titulo="t", descripcion="d")
        servicio.assert_not_called()

    def test_mix_final_escribe_el_sello(self):
        v = os.path.join(self.dir, "v.mp4")
        _mp4(v)
        voz = os.path.join(self.dir, "voz.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=f=220:d=1", voz], check=True)
        out = os.path.join(self.dir, "out.mp4")
        audio_mix.mix_final(v, voz, voz, out, metadata=sello_musica.args_sello(
            sello_musica.MUSICA_LIBRE, ["mystery_low_01"]))
        self.assertEqual(sello_musica.exigir_sello(out)["tracks"], "mystery_low_01")


if __name__ == "__main__":
    unittest.main()
