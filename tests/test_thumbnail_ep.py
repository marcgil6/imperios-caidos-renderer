"""Tests del titular en acabado "cartel de impacto".

Lo que se mide aqui son las dos proporciones que hacen que el acabado se lea, y
que no se ven leyendo el codigo:

1. El relieve tiene que ESCALAR CON EL CUERPO de letra. Con un grosor fijo en
   pixeles, la linea corta (cap 48) recibia el mismo contorno de 7 px que el
   titular grande (cap 86) y salia una plancha negra que se comia las letras.
   Este es el fallo que de verdad se veia.
2. La profundidad tiene que superar al contorno, porque el contorno de la cara
   se dibuja DESPUES de la extrusion y le come el filo. Pesa menos que lo
   anterior, pero es la diferencia entre un relieve que se aprecia y uno que
   queda en un borde de 1 px.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import thumbnail_ep as te


def _cuenta_color(capa, rgb, tol=26):
    """Pixeles opacos cercanos a un color."""
    a = np.asarray(capa).astype(np.int16)
    op = a[:, :, 3] > 200
    cerca = np.all(np.abs(a[:, :, :3] - np.array(rgb, dtype=np.int16)) <= tol, axis=2)
    return int(np.count_nonzero(op & cerca))


def _granate(capa):
    """Toda la familia del relieve, no un tono suelto: la extrusion es un
    degradado de #6B0F05 a #3A0602 y contar solo un extremo deja fuera casi
    toda la banda."""
    a = np.asarray(capa).astype(np.int16)
    op = a[:, :, 3] > 200
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return int(np.count_nonzero(op & (r >= 30) & (r <= 150) &
                                (g < 50) & (b < 45) & (r > g + 22)))


class TestAcabadoImpacto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not te.FUENTE_OK:
            raise unittest.SkipTest("falta Anton-Regular.ttf")
        cls.f = te.fuente_cap(86)

    def test_la_extrusion_se_ve(self):
        capa, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        self.assertGreater(_granate(capa), 3000)

    def test_mas_profundidad_que_contorno_da_mas_relieve(self):
        # Medido: con contorno 7+3 contra profundidad 8 el relieve queda en un
        # filo; con 6+3 contra 15 se abre una banda de verdad. No desaparece,
        # se estrecha — de ahi que el test compare y no busque un cero.
        prod, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        plano, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA,
                                      contorno=7, filete=3, profundidad=8)
        self.assertGreater(_granate(prod), _granate(plano) * 1.25)

    def test_los_valores_de_produccion_respetan_la_regla(self):
        self.assertGreater(te.IMP_PROF, te.IMP_CONTORNO + te.IMP_FILETE)

    def test_filete_rojo_presente(self):
        capa, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        self.assertGreater(_cuenta_color(capa, te.IMPACTO_FILETE, tol=34), 300)

    def test_blanca_y_amarilla_comparten_acabado(self):
        # Lo unico que puede cambiar entre las dos es la cara. Si alguien mete
        # un relieve gris para la blanca, las dos lineas dejan de leerse como
        # un mismo rotulo.
        a, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        b, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_BLANCA)
        for capa in (a, b):
            self.assertGreater(_cuenta_color(capa, te.IMPACTO_FILETE, tol=34), 300)
            self.assertGreater(_granate(capa), 3000)

    def test_la_cara_lleva_su_degradado(self):
        capa, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        self.assertGreater(_cuenta_color(capa, te._rgb(te.CARA_AMARILLA[1]), tol=45), 800)
        blanca, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_BLANCA)
        self.assertLess(_cuenta_color(blanca, te._rgb(te.CARA_AMARILLA[1]), tol=45), 200)

    def test_grietas_reproducibles(self):
        # Se siembran con el texto: dos renders de la misma frase deben ser
        # identicos, o cada recomposicion daria una miniatura distinta.
        a, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        b, _, _ = te.capa_impacto("LADRONES", self.f, te.CARA_AMARILLA)
        self.assertTrue(np.array_equal(np.asarray(a), np.asarray(b)))

    def test_el_relieve_escala_con_el_cuerpo(self):
        # Una linea pequena no puede llevar el mismo grosor en px que el
        # titular grande: se ahogaria.
        from PIL import Image
        grande = Image.new("RGBA", (te.W, te.H), (0, 0, 0, 0))
        pequena = Image.new("RGBA", (te.W, te.H), (0, 0, 0, 0))
        te.pegar_impacto(grande, (60, 300), "ABC", te.fuente_cap(86), te.CARA_AMARILLA)
        te.pegar_impacto(pequena, (60, 300), "ABC", te.fuente_cap(40), te.CARA_AMARILLA)
        self.assertGreater(_granate(grande), _granate(pequena) * 1.5)

    def test_el_contorno_no_se_come_la_linea_pequena(self):
        # El fallo que se veia: contorno fijo de 7 px sobre un cap de 48
        # convertia la linea corta en una plancha negra. Escalado, el negro
        # baja y el relieve granate aparece.
        f = te.fuente_cap(48)
        escalada, _, _ = te.capa_impacto("EGIPTO, 1881", f, te.CARA_BLANCA,
                                         **te._medidas_para(f))
        fija, _, _ = te.capa_impacto("EGIPTO, 1881", f, te.CARA_BLANCA,
                                     contorno=7, filete=3, profundidad=8)
        self.assertLess(_cuenta_color(escalada, (0, 0, 0), tol=20),
                        _cuenta_color(fija, (0, 0, 0), tol=20))
        self.assertGreater(_granate(escalada), _granate(fija))


class TestFuentes(unittest.TestCase):
    def test_wordmark_cae_a_anton_sin_impact(self):
        # Impact es una fuente del sistema de macOS y no existe en el
        # contenedor. Sin fallback, la composicion reventaba en el servidor.
        self.assertTrue(te.FUENTE_MARCA)
        self.assertTrue(os.path.exists(te.FUENTE_MARCA))


if __name__ == "__main__":
    unittest.main()
