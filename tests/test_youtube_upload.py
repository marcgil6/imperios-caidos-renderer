"""Tests de la subida programada a YouTube.

Lo que se prueba aqui es la parte que decide QUE se le manda a la API, porque es
donde se pierde el dinero: cada videos.insert cuesta 1.600 unidades de una cuota
diaria de 10.000 y una subida mal formada hay que repetirla entera con 1,4 GB.
YouTube ademas no deja sustituir el fichero de un video ya subido (lo aprendimos
el 18/09 re-subiendo los cuatro Shorts sin marca), asi que equivocarse obliga a
subir otro video y dejar el anterior en privado.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import youtube_upload


class TestNormalizeTags(unittest.TestCase):
    def test_cadena_de_airtable(self):
        # Airtable guarda tags_youtube como una cadena separada por comas.
        tags = youtube_upload._normalize_tags(
            "historia antigua, misterios arqueologicos ,  civilizaciones antiguas"
        )
        self.assertEqual(
            tags,
            ["historia antigua", "misterios arqueologicos", "civilizaciones antiguas"],
        )

    def test_quita_almohadillas_y_duplicados(self):
        # El prompt de EP-05 pide tags SIN almohadilla, pero GPT-4o las cuela a
        # veces; YouTube las rechazaria o las dejaria como parte del texto.
        self.assertEqual(
            youtube_upload._normalize_tags(["#egipto", "egipto", "  ", "sahara"]),
            ["egipto", "sahara"],
        )

    def test_vacio(self):
        self.assertEqual(youtube_upload._normalize_tags(None), [])
        self.assertEqual(youtube_upload._normalize_tags(""), [])

    def test_recorta_por_etiqueta_entera(self):
        # YouTube corta el campo a 500 caracteres. Si cortamos nosotros por
        # caracteres, la ultima etiqueta entra partida a media palabra.
        tags = youtube_upload._normalize_tags([f"etiqueta-de-prueba-{i}" for i in range(60)])
        self.assertLess(sum(len(t) + 1 for t in tags), 500)
        self.assertTrue(all(t.startswith("etiqueta-de-prueba-") for t in tags))


class TestUploadBody(unittest.TestCase):
    """El cuerpo que se le manda a videos.insert."""

    def _upload(self, tmp_path, **kwargs):
        fake_yt = mock.MagicMock()
        insert = fake_yt.videos.return_value.insert
        insert.return_value.next_chunk.return_value = (None, {"id": "ABC123"})
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt), \
                mock.patch.object(youtube_upload, "MediaFileUpload"):
            result = youtube_upload.upload(tmp_path, **kwargs)
        return result, insert.call_args.kwargs["body"]

    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__), "_yt_dummy.mp4")
        with open(self.path, "wb") as f:
            f.write(b"0" * 1024)
        # El fichero de prueba no es un MP4 de verdad: el candado de musica se
        # prueba aparte (test_sello_musica), aqui se da por superado.
        p = mock.patch.object(youtube_upload.sello_musica, "exigir_sello",
                              return_value={"ep-music": "yt-audio-library"})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        os.remove(self.path)

    def test_programado_va_en_privado(self):
        # La regla de la API: con publishAt el video DEBE estar en privado.
        # Si se sube en public, YouTube ignora la fecha y lo publica ya.
        result, body = self._upload(
            self.path, titulo="Los papiros de Herculano",
            descripcion="texto", publish_at="2026-10-01T16:00:00Z",
        )
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2026-10-01T16:00:00Z")
        self.assertEqual(result["video_id"], "ABC123")
        self.assertEqual(result["quota"], youtube_upload.QUOTA_INSERT)

    def test_sin_fecha_no_manda_publishat(self):
        _, body = self._upload(self.path, titulo="t", descripcion="d")
        self.assertNotIn("publishAt", body["status"])
        self.assertEqual(body["status"]["privacyStatus"], "private")

    def test_metadatos_del_canal(self):
        # Hay que imitar los del canal o el video se sale de la serie:
        # categoria 24, idioma es, no hecho para ninos.
        _, body = self._upload(self.path, titulo="t", descripcion="d")
        self.assertEqual(body["snippet"]["categoryId"], "24")
        self.assertEqual(body["snippet"]["defaultLanguage"], "es")
        self.assertEqual(body["snippet"]["defaultAudioLanguage"], "es")
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])

    def test_recorta_titulo_y_descripcion(self):
        # YouTube rechaza la subida entera si el titulo pasa de 100 caracteres.
        _, body = self._upload(self.path, titulo="A" * 250, descripcion="B" * 9000)
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertEqual(len(body["snippet"]["description"]), 5000)

    def test_miniatura_que_falla_no_tumba_la_subida(self):
        # 1.600 unidades ya gastadas: perderlas porque el PNG dio error seria
        # absurdo cuando la miniatura se puede poner despues.
        from googleapiclient.errors import HttpError

        fake_yt = mock.MagicMock()
        fake_yt.videos.return_value.insert.return_value.next_chunk.return_value = (
            None, {"id": "XYZ"})
        fake_yt.thumbnails.return_value.set.side_effect = HttpError(
            mock.Mock(status=403), b"forbidden")
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt), \
                mock.patch.object(youtube_upload, "MediaFileUpload"):
            result = youtube_upload.upload(
                self.path, titulo="t", descripcion="d", thumbnail_path=self.path)
        self.assertEqual(result["video_id"], "XYZ")
        self.assertFalse(result["thumbnail_set"])


class TestUnschedule(unittest.TestCase):
    def test_conserva_el_resto_del_status(self):
        # videos.update con part=status REEMPLAZA el objeto entero: si no se
        # reenvian license y publicStatsViewable, se pierden.
        fake_yt = mock.MagicMock()
        fake_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {
                "privacyStatus": "private",
                "publishAt": "2026-10-01T16:00:00Z",
                "license": "creativeCommon",
                "publicStatsViewable": False,
                "selfDeclaredMadeForKids": False,
                "uploadStatus": "processed",
            }}]
        }
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt):
            youtube_upload.unschedule("ABC123")

        body = fake_yt.videos.return_value.update.call_args.kwargs["body"]
        self.assertNotIn("publishAt", body["status"])
        self.assertNotIn("uploadStatus", body["status"])
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["license"], "creativeCommon")
        self.assertIs(body["status"]["publicStatsViewable"], False)

    def test_video_inexistente(self):
        fake_yt = mock.MagicMock()
        fake_yt.videos.return_value.list.return_value.execute.return_value = {"items": []}
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt):
            with self.assertRaises(ValueError):
                youtube_upload.unschedule("NOPE")


class TestPublishNow(unittest.TestCase):
    def _fake(self, status):
        fake_yt = mock.MagicMock()
        fake_yt.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": status}]}
        return fake_yt

    def test_pasa_a_publico_y_quita_la_fecha(self):
        fake_yt = self._fake({"privacyStatus": "private", "publishAt": "2026-10-03T16:00:00Z",
                              "license": "youtube", "publicStatsViewable": True,
                              "selfDeclaredMadeForKids": False, "uploadStatus": "processed"})
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt):
            r = youtube_upload.publish_now("N99")
        body = fake_yt.videos.return_value.update.call_args.kwargs["body"]
        self.assertEqual(body["status"]["privacyStatus"], "public")
        self.assertNotIn("publishAt", body["status"])
        self.assertNotIn("uploadStatus", body["status"])
        self.assertIs(body["status"]["selfDeclaredMadeForKids"], False)
        self.assertEqual(r["privacy"], "public")

    def test_no_publica_un_video_sin_procesar(self):
        fake_yt = self._fake({"privacyStatus": "private", "uploadStatus": "failed"})
        with mock.patch.object(youtube_upload, "_service", return_value=fake_yt):
            with self.assertRaises(ValueError):
                youtube_upload.publish_now("N99")
        fake_yt.videos.return_value.update.assert_not_called()


BUENAS = {
    "YT_CLIENT_ID": "595589045581-abc.apps.googleusercontent.com",
    "YT_CLIENT_SECRET": "GOCSPX-" + "x" * 24,
    "YT_REFRESH_TOKEN": "1//" + "y" * 60,
}


class TestCredentials(unittest.TestCase):
    def test_faltan_credenciales(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(youtube_upload.credentials_configured())
            with self.assertRaises(youtube_upload.YouTubeAuthError):
                youtube_upload._service()

    def test_las_tres_son_obligatorias(self):
        parcial = dict(BUENAS); parcial.pop("YT_REFRESH_TOKEN")
        with mock.patch.dict(os.environ, parcial, clear=True):
            self.assertFalse(youtube_upload.credentials_configured())

    def test_credenciales_con_buena_forma(self):
        with mock.patch.dict(os.environ, BUENAS, clear=True):
            self.assertTrue(youtube_upload.credentials_configured())
            self.assertEqual(youtube_upload.credentials_problems(), [])

    def test_valor_basura_no_cuenta_como_configurado(self):
        # Paso de verdad el 22/09: las tres variables llegaron con el mismo
        # valor de 12 caracteres y /health lo dio por bueno, que es peor que
        # decir que no: da luz verde a una subida que muere en la
        # autenticacion DESPUES de bajar 1,4 GB de Drive.
        basura = {k: "False False" for k in BUENAS}
        with mock.patch.dict(os.environ, basura, clear=True):
            self.assertFalse(youtube_upload.credentials_configured())
            self.assertEqual(len(youtube_upload.credentials_problems()), 3)

    def test_el_problema_dice_que_se_esperaba(self):
        malo = dict(BUENAS, YT_REFRESH_TOKEN="ya29.deberia-ser-un-refresh-token")
        with mock.patch.dict(os.environ, malo, clear=True):
            problemas = youtube_upload.credentials_problems()
            self.assertEqual(len(problemas), 1)
            self.assertIn("1//", problemas[0])


if __name__ == "__main__":
    unittest.main()
