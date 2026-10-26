"""Cifrado de app_settings: llave propia DRIVE_TOKEN_KEY, fallback derivado y re-cifrado perezoso."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

import app_settings

SESION_A = "session-secret-A-" + "a" * 32
SESION_B = "session-secret-B-" + "b" * 32
DRIVE_KEY = "drive-token-key-" + "d" * 32


def _env(**values):
    base = {"ERP_SESSION_SECRET": "", "DRIVE_TOKEN_KEY": ""}
    base.update(values)
    return mock.patch.dict(os.environ, base)


class FakeCursor:
    def __init__(self, store, key_row_enc=None):
        self.store = store
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "information_schema.tables" in s:
            self._result = (1,)
        elif s.startswith("SELECT value_encrypted"):
            enc = self.store.get(params[0])
            self._result = {"value_encrypted": enc} if enc is not None else None
        elif s.startswith("UPDATE app_settings"):
            nuevo, key, viejo = params
            if self.store.get(key) == viejo:
                self.store[key] = nuevo
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.released = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.store)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def release(self):
        self.released = True


class CifradoTests(unittest.TestCase):
    def test_sin_llave_dedicada_usa_derivada_como_antes(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            enc = app_settings.encrypt_value("rt-1")
            self.assertEqual(app_settings.decrypt_value(enc), "rt-1")
            self.assertEqual(app_settings._decrypt_con_origen(enc)[1], "derivada")

    def test_con_llave_dedicada_cifra_con_ella(self):
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            enc = app_settings.encrypt_value("rt-1")
            self.assertEqual(app_settings._decrypt_con_origen(enc), ("rt-1", "dedicada"))
        with _env(ERP_SESSION_SECRET=SESION_B, DRIVE_TOKEN_KEY=DRIVE_KEY):
            self.assertEqual(app_settings.decrypt_value(enc), "rt-1")

    def test_rotar_sesion_no_rompe_con_llave_dedicada(self):
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            enc = app_settings.encrypt_value("rt-secreto")
        with _env(ERP_SESSION_SECRET=SESION_B, DRIVE_TOKEN_KEY=DRIVE_KEY):
            self.assertEqual(app_settings.decrypt_value(enc), "rt-secreto")

    def test_valor_viejo_cifrado_con_sesion_se_lee_con_dedicada_configurada(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            viejo = app_settings.encrypt_value("rt-viejo")
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            self.assertEqual(app_settings._decrypt_con_origen(viejo), ("rt-viejo", "derivada"))

    def test_llave_incorrecta_falla(self):
        from cryptography.fernet import InvalidToken

        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            enc = app_settings.encrypt_value("x")
        with _env(ERP_SESSION_SECRET=SESION_B, DRIVE_TOKEN_KEY="otra-llave-" + "z" * 32):
            with self.assertRaises(InvalidToken):
                app_settings.decrypt_value(enc)

    def test_llave_dedicada_corta_se_rechaza(self):
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY="corta"):
            with self.assertRaises(RuntimeError):
                app_settings.encrypt_value("x")

    def test_sin_ninguna_llave_falla_cerrado(self):
        with _env():
            with self.assertRaises(RuntimeError):
                app_settings.encrypt_value("x")


class MigracionPerezosaTests(unittest.TestCase):
    KEY = app_settings.KEY_GOOGLE_OAUTH_REFRESH

    def _get(self, store):
        conn = FakeConn(store)
        with mock.patch("app_settings.db.get_connection", return_value=conn):
            return app_settings.get_setting(self.KEY), conn

    def test_lectura_con_vieja_recifra_con_nueva(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
        original = store[self.KEY]
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            valor, conn = self._get(store)
            self.assertEqual(valor, "rt-1")
            self.assertNotEqual(store[self.KEY], original)
            self.assertEqual(app_settings._decrypt_con_origen(store[self.KEY])[1], "dedicada")
            self.assertTrue(conn.released)
        # Tras migrar, rotar la sesión ya no rompe la lectura
        with _env(ERP_SESSION_SECRET=SESION_B, DRIVE_TOKEN_KEY=DRIVE_KEY):
            valor, _ = self._get(store)
            self.assertEqual(valor, "rt-1")

    def test_sin_llave_dedicada_no_recifra(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
            original = store[self.KEY]
            valor, _ = self._get(store)
        self.assertEqual(valor, "rt-1")
        self.assertEqual(store[self.KEY], original)

    def test_ya_con_dedicada_no_toca_la_fila(self):
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
            original = store[self.KEY]
            valor, _ = self._get(store)
        self.assertEqual(valor, "rt-1")
        self.assertEqual(store[self.KEY], original)

    def test_fallo_al_recifrar_no_rompe_la_lectura(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY), mock.patch(
            "app_settings.encrypt_value", side_effect=RuntimeError("boom")
        ):
            valor, _ = self._get(store)
        self.assertEqual(valor, "rt-1")

    def test_sin_perder_valor_si_sesion_rotada_antes_de_dedicada(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
        with _env(ERP_SESSION_SECRET=SESION_B):
            valor, _ = self._get(store)
        self.assertIsNone(valor)

    def test_cipher_status(self):
        with _env(ERP_SESSION_SECRET=SESION_A):
            store = {self.KEY: app_settings.encrypt_value("rt-1")}
            with mock.patch("app_settings.db.get_connection", return_value=FakeConn(store)):
                self.assertEqual(app_settings.cipher_status(self.KEY), "derivada")
        with _env(ERP_SESSION_SECRET=SESION_A, DRIVE_TOKEN_KEY=DRIVE_KEY):
            with mock.patch("app_settings.db.get_connection", return_value=FakeConn(store)):
                self.assertEqual(app_settings.cipher_status(self.KEY), "derivada")
                app_settings.get_setting(self.KEY)
                self.assertEqual(app_settings.cipher_status(self.KEY), "dedicada")


if __name__ == "__main__":
    unittest.main()
