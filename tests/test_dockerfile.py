"""El Dockerfile copia los modulos uno a uno: si render.py importa un .py local que no se copia,
gunicorn no arranca y el renderer entero se cae (paso el 14/10/2026 con thumbnail_hibrida.py)."""

import ast
import os
import re
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDockerfile(unittest.TestCase):
    def test_render_importa_solo_lo_que_se_copia(self):
        arbol = ast.parse(open(os.path.join(RAIZ, "render.py"), encoding="utf-8").read())
        modulos = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Import):
                modulos.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                modulos.add(n.module.split(".")[0])
        locales = {m for m in modulos if os.path.exists(os.path.join(RAIZ, m + ".py"))
                   or os.path.isdir(os.path.join(RAIZ, m))}
        docker = open(os.path.join(RAIZ, "Dockerfile"), encoding="utf-8").read()
        copiados = {c.rstrip("/").removesuffix(".py") for c in re.findall(r"^COPY\s+(\S+)", docker, re.M)}
        self.assertEqual(sorted(locales - copiados), [])


if __name__ == "__main__":
    unittest.main()
