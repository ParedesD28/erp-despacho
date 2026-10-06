"""Regresión: guardar anotaciones CRM (/crm/guardar).

Causa histórica: crm_guardar usaba cursor por defecto (tuplas) pero leía
filas con claves de texto → TypeError y redirect de error en casi todo guardado.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import main
import permisos


class _FakeCursor:
    """Simula RealDictCursor: fetchone/fetchall devolviendo dicts."""

    def __init__(self, responses=None):
        self.statements = []
        self._responses = list(responses or [])
        self._idx = 0
        self.cursor_factory = None

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        if self._idx < len(self._responses):
            value = self._responses[self._idx]
            self._idx += 1
            return value
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.released = False
        self.cursor_kwargs = None

    def cursor(self, **kwargs):
        self.cursor_kwargs = kwargs
        self._cursor.cursor_factory = kwargs.get("cursor_factory")
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def release(self):
        self.released = True


class CrmGuardarGestionTests(unittest.TestCase):
    def test_formulario_crm_envia_contexto_sin_inmueble_vacio(self):
        html = (ROOT / "templates" / "crm.html").read_text(encoding="utf-8")
        self.assertIn('action="/crm/guardar"', html)
        self.assertIn('name="radicado_interno"', html)
        self.assertIn('name="conjunto_id"', html)
        self.assertIn('name="obligacion_id"', html)
        # No enviar inmueble_id="" (antes rompía parsers más estrictos).
        self.assertNotIn("cuenta_actual.inmueble_id or ''", html)
        self.assertIn("{% if cuenta_actual.inmueble_id %}", html)

    def test_crm_guardar_usa_realdic_tcursor(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("def crm_guardar(", source)
        # El bloque de guardado debe pedir RealDictCursor (no cursor() a pelo).
        idx = source.index("def crm_guardar(")
        bloque = source[idx : idx + 3500]
        self.assertIn("cursor_factory=RealDictCursor", bloque)
        self.assertIn("_crm_identificacion_vinculada", bloque)

    def test_guarda_gestion_resolviendo_obligacion_por_nombre_columna(self):
        cur = _FakeCursor(
            responses=[
                {"obligacion_id": 77},
                {"id": 77, "inmueble_id": 501},
                {"ok": 1},  # vínculo proceso_obligaciones
                {"ok": 1},  # identificación vinculada (deudor)
                None,  # INSERT no fetch
            ]
        )
        conn = _FakeConn(cur)
        request = MagicMock()

        with patch.object(main.db, "get_connection", return_value=conn):
            with patch.object(main, "_redirect", return_value="redir") as redir:
                with patch.object(
                    main.expedientes_service, "_table_exists", return_value=True
                ):
                    resp = main.crm_guardar(
                        request,
                        radicado_interno="EXP-0065",
                        conjunto_id=35,
                        inmueble_id=501,
                        obligacion_id=None,
                        tipo_contacto="Llamada Telefónica",
                        resumen="Cliente atendió y promete pago",
                        promesa_pago_fecha=None,
                        identificacion_deudor="1234567890",
                    )

        self.assertEqual(resp, "redir")
        self.assertTrue(conn.released)
        self.assertIsNotNone(conn.cursor_kwargs)
        self.assertEqual(
            conn.cursor_kwargs.get("cursor_factory"),
            main.RealDictCursor,
        )
        insert_sql = cur.statements[-1][0]
        self.assertIn("INSERT INTO gestiones_crm", insert_sql)
        insert_params = cur.statements[-1][1]
        self.assertEqual(insert_params[0], "EXP-0065")
        self.assertEqual(insert_params[1], 501)
        self.assertEqual(insert_params[2], 77)
        self.assertEqual(insert_params[3], "Llamada Telefónica")
        redir.assert_called_once()
        kwargs = redir.call_args.kwargs
        self.assertEqual(kwargs.get("mensaje"), "Gestión registrada")
        self.assertEqual(kwargs.get("radicado_interno"), "EXP-0065")
        self.assertEqual(kwargs.get("conjunto_id"), 35)

    def test_falla_clara_si_cursor_devolviera_tupla_sin_dict(self):
        """Documenta el bug: con tupla, row_ob['obligacion_id'] revienta."""
        row_ob = (77,)
        with self.assertRaises(TypeError):
            _ = int(row_ob["obligacion_id"])

    def test_identificacion_acepta_demandado_del_proceso(self):
        cur = _FakeCursor(
            responses=[
                None,  # no en obligacion_partes
                {"ok": 1},  # sí en proceso_partes DEMANDADO
            ]
        )
        with patch.object(main.expedientes_service, "_table_exists", return_value=True):
            ok = main._crm_identificacion_vinculada(
                cur,
                ident="1098765432",
                obligacion_id=10,
                radicado="EXP-0001",
                inmueble_id=None,
            )
        self.assertTrue(ok)
        self.assertIn("obligacion_partes", cur.statements[0][0])
        self.assertIn("proceso_partes", cur.statements[1][0])

    def test_auxiliar_y_abogado_pueden_post_crm_guardar(self):
        for perfil in (permisos.PERFIL_AUXILIAR, permisos.PERFIL_ABOGADO):
            bag = permisos.permisos_de_perfil(perfil)
            self.assertFalse(
                permisos.denegar_acceso(bag, "POST", "/crm/guardar"),
                msg=f"{perfil} debería poder POST /crm/guardar",
            )
        consulta = permisos.permisos_de_perfil(permisos.PERFIL_CONSULTA)
        self.assertTrue(permisos.denegar_acceso(consulta, "POST", "/crm/guardar"))


if __name__ == "__main__":
    unittest.main()
