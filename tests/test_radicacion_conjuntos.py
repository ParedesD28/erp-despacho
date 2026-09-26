"""Regresión: radicación PH/conjuntos resuelve demandante desde el conjunto."""
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

import radicacion_service


def _base_kwargs(**overrides):
    data = {
        "naturaleza": "EJECUTIVO",
        "tipo_obligacion_codigo": "CUOTAS_ADMINISTRACION",
        "tipo_cartera": "PREJURIDICO",
        "radicado_rama": "",
        "juzgado": None,
        "apto": "TORRE 1 APTO 101",
        "conjunto_id_raw": "7",
        "conjunto_nombre": "",
        "abogado_id": None,
        "medidas": "",
        "pretensiones": 0,
        "capital_titulo": 0,
        "documento_referencia": "",
        "fecha_exigibilidad": "",
        "demandantes": [],
        "nuevos_dem": [],
        "demandados": ["900100200"],
        "nuevos_ddo": [],
    }
    data.update(overrides)
    return data


class _FakeCursor:
    """Cursor mínimo para el camino feliz de CUOTAS_ADMINISTRACION."""

    def __init__(self):
        self.statements = []
        self._fetchone_queue = []
        self._fetchall_queue = []

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.statements.append((normalized, params))

        # Radicado interno
        if "pg_advisory_xact_lock" in normalized:
            return
        if "MAX(CAST(SUBSTRING(radicado_interno" in normalized:
            self._fetchone_queue.append({"max": 41})
            return

        # Acreedor del conjunto
        if "FROM contactos" in normalized and "WHERE id=%s" in normalized:
            self._fetchone_queue.append(
                {
                    "id": 55,
                    "identificacion": "800123456-1",
                    "nombre": "PH CONJUNTO DEMO",
                }
            )
            return

        # Contactos de partes
        if "FROM contactos" in normalized and "WHERE identificacion IN" in normalized:
            self._fetchall_queue.append(
                [
                    {
                        "id": 55,
                        "identificacion": "800123456-1",
                        "nombre": "PH CONJUNTO DEMO",
                    },
                    {
                        "id": 88,
                        "identificacion": "900100200",
                        "nombre": "DEUDOR DEMO",
                    },
                ]
            )
            return

        # Inmueble existente
        if "FROM inmuebles_ph" in normalized:
            self._fetchone_queue.append({"id": 301})
            return

        # INSERT / UPDATE: sin fetch obligatorio
        return

    def fetchone(self):
        if self._fetchone_queue:
            return self._fetchone_queue.pop(0)
        return None

    def fetchall(self):
        if self._fetchall_queue:
            return self._fetchall_queue.pop(0)
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.released = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def release(self):
        self.released = True


class RadicacionConjuntosTests(unittest.TestCase):
    def test_ui_ph_oculta_demandante_y_usa_conjunto(self):
        html = (ROOT / "templates" / "procesos.html").read_text(encoding="utf-8")
        self.assertIn("aviso-demandante-automatico", html)
        self.assertIn("bloque-demandante", html)
        self.assertIn("selectDemandantes.clear()", html)
        self.assertIn("CUOTAS_ADMINISTRACION", html)
        self.assertIn('name="conjunto_id"', html)

    def test_precheck_ya_no_exige_demandante_antes_del_conjunto(self):
        """Regresión producción: ValueError('Debe existir al menos un demandante')
        ocurría antes de resolver el acreedor PH del conjunto."""
        source = (ROOT / "radicacion_service.py").read_text(encoding="utf-8")
        # El chequeo de demandante debe quedar DESPUÉS del bloque requiere_conjunto.
        idx_conjunto = source.find('tipo_obligacion["requiere_conjunto"]')
        idx_demandante = source.find(
            'raise ValueError("Debe existir al menos un demandante")'
        )
        self.assertGreater(idx_conjunto, 0)
        self.assertGreater(idx_demandante, idx_conjunto)

        reached_db = {"ok": False}

        def boom_conn():
            reached_db["ok"] = True
            raise RuntimeError("stop-after-precheck")

        with patch.object(radicacion_service.db, "get_connection", side_effect=boom_conn):
            with self.assertRaisesRegex(RuntimeError, "stop-after-precheck"):
                radicacion_service.radicar_proceso(**_base_kwargs(demandantes=[]))

        self.assertTrue(reached_db["ok"])

    def test_sin_demandante_ni_conjunto_sigue_fallando(self):
        """Otros tipos de proceso siguen exigiendo demandante."""
        cur = _FakeCursor()
        conn = _FakeConn(cur)

        with patch.object(radicacion_service.db, "get_connection", return_value=conn):
            with patch.object(
                radicacion_service.catalogos_service,
                "obtener_tipo_proceso",
                return_value={"id": 1, "codigo": "EJECUTIVO"},
            ):
                with patch.object(
                    radicacion_service.catalogos_service,
                    "obtener_tipo_obligacion",
                    return_value={
                        "id": 2,
                        "codigo": "PAGARE",
                        "requiere_conjunto": False,
                        "requiere_inmueble": False,
                        "requiere_documento": True,
                    },
                ):
                    with self.assertRaises(ValueError) as ctx:
                        radicacion_service.radicar_proceso(
                            **_base_kwargs(
                                tipo_obligacion_codigo="PAGARE",
                                conjunto_id_raw="",
                                apto="",
                                documento_referencia="PAG-1",
                                capital_titulo=1_000_000,
                                demandantes=[],
                            )
                        )
        self.assertIn("demandante", str(ctx.exception).lower())

    def test_cuotas_resuelve_demandante_desde_persona_juridica_del_conjunto(self):
        cur = _FakeCursor()
        conn = _FakeConn(cur)
        created = {}

        def fake_crear_obligacion(cursor, **kwargs):
            created["acreedor"] = kwargs["acreedor"]
            created["deudor"] = kwargs["deudor"]
            created["inmueble_id"] = kwargs["inmueble_id"]
            return 999

        with patch.object(radicacion_service.db, "get_connection", return_value=conn):
            with patch.object(
                radicacion_service.catalogos_service,
                "obtener_tipo_proceso",
                return_value={"id": 1, "codigo": "EJECUTIVO"},
            ):
                with patch.object(
                    radicacion_service.catalogos_service,
                    "obtener_tipo_obligacion",
                    return_value={
                        "id": 3,
                        "codigo": "CUOTAS_ADMINISTRACION",
                        "requiere_conjunto": True,
                        "requiere_inmueble": True,
                        "requiere_documento": False,
                    },
                ):
                    with patch.object(
                        radicacion_service.catalogos_service,
                        "obtener_conjunto",
                        return_value={
                            "id": 7,
                            "nombre": "CONJUNTO DEMO",
                            "contacto_id": 55,
                        },
                    ):
                        with patch.object(
                            radicacion_service.expedientes_service,
                            "_cols",
                            return_value={
                                "radicado_interno",
                                "radicado_rama",
                                "estado_rama",
                                "tipo_cartera",
                                "tipo_proceso_id",
                                "naturaleza",
                                "etapa_actual",
                                "juzgado",
                                "estado",
                                "inmueble_id",
                                "pretensiones",
                                "medidas_cautelares",
                                "abogado_id",
                            },
                        ):
                            with patch.object(
                                radicacion_service.expedientes_service,
                                "_table_exists",
                                return_value=True,
                            ):
                                with patch.object(
                                    radicacion_service.obligaciones_service,
                                    "crear_obligacion",
                                    side_effect=fake_crear_obligacion,
                                ):
                                    with patch.object(
                                        radicacion_service.obligaciones_service,
                                        "vincular_partes_obligacion",
                                    ):
                                        with patch.object(
                                            radicacion_service.obligaciones_service,
                                            "vincular_obligacion_a_proceso",
                                        ):
                                            resultado = radicacion_service.radicar_proceso(
                                                **_base_kwargs(demandantes=[])
                                            )

        self.assertEqual(resultado["radicado_interno"], "EXP-0042")
        self.assertEqual(resultado["obligacion_id"], 999)
        self.assertEqual(resultado["tipo_obligacion"], "CUOTAS_ADMINISTRACION")
        self.assertEqual(created["acreedor"]["identificacion"], "800123456-1")
        self.assertEqual(created["deudor"]["identificacion"], "900100200")
        self.assertEqual(created["inmueble_id"], 301)
        self.assertTrue(conn.released)

        partes_demandante = [
            params
            for sql, params in cur.statements
            if "INSERT INTO proceso_partes" in sql and "'DEMANDANTE'" in sql
        ]
        self.assertEqual(len(partes_demandante), 1)
        self.assertEqual(partes_demandante[0][1], 55)

    def test_cuotas_rechaza_demandante_manual(self):
        cur = _FakeCursor()
        conn = _FakeConn(cur)

        with patch.object(radicacion_service.db, "get_connection", return_value=conn):
            with patch.object(
                radicacion_service.catalogos_service,
                "obtener_tipo_proceso",
                return_value={"id": 1, "codigo": "EJECUTIVO"},
            ):
                with patch.object(
                    radicacion_service.catalogos_service,
                    "obtener_tipo_obligacion",
                    return_value={
                        "id": 3,
                        "codigo": "CUOTAS_ADMINISTRACION",
                        "requiere_conjunto": True,
                        "requiere_inmueble": True,
                        "requiere_documento": False,
                    },
                ):
                    with self.assertRaises(ValueError) as ctx:
                        radicacion_service.radicar_proceso(
                            **_base_kwargs(demandantes=["111"])
                        )
        self.assertIn("acreedor se toma del conjunto", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
