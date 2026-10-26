"""Llaves M2M separadas: transición con llave heredada, fail-closed y tiempo constante."""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "ERP_SESSION_SECRET",
    "ci-dummy-secret-not-used-in-production-000000",
)

from fastapi import HTTPException

import agent_supervision
import api_recaudos
import bot_api
import m2m_keys

OLD = "old-shared-key-AAAAAAAAAAAAAAAAAAAA"
NEW_BOT = "new-bot-key-BBBBBBBBBBBBBBBBBBBBBBB"
NEW_REC = "new-recaudos-key-CCCCCCCCCCCCCCCCC"
NEW_PDF = "new-pdf-secret-DDDDDDDDDDDDDDDDDDD"
NEW_SUP = "new-supervision-key-EEEEEEEEEEEEEEE"

M2M_VARS = (
    "LIQUIDADOR_API_KEY",
    "BOT_API_KEY",
    "RECAUDOS_API_KEY",
    "BOT_PDF_PUBLIC_SECRET",
    "AGENT_SUPERVISION_KEY",
    "M2M_LEGACY_KEY_ENABLED",
)


def _env(**values):
    base = {k: "" for k in M2M_VARS}
    base.update(values)
    return mock.patch.dict(os.environ, base)


def _req(key=None):
    headers = {} if key is None else {"X-API-Key": key}
    return SimpleNamespace(headers=headers)


def _status(fn, *args):
    try:
        fn(*args)
    except HTTPException as exc:
        return exc.status_code
    return 200


class ApiKeyMatrixMixin:
    """Matriz común: solo-vieja, solo-nueva, ambas, incorrecta y vacía."""

    purpose_env = ""
    new_value = ""

    def check(self, request_key):
        raise NotImplementedError

    def test_solo_llave_vieja(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self.check(OLD), 200)
            self.assertEqual(self.check(self.new_value), 401)

    def test_solo_llave_nueva(self):
        with _env(**{self.purpose_env: self.new_value}):
            self.assertEqual(self.check(self.new_value), 200)
            self.assertEqual(self.check(OLD), 401)

    def test_ambas_aceptan_las_dos(self):
        with _env(LIQUIDADOR_API_KEY=OLD, **{self.purpose_env: self.new_value}):
            self.assertEqual(self.check(OLD), 200)
            self.assertEqual(self.check(self.new_value), 200)

    def test_llave_incorrecta(self):
        with _env(LIQUIDADOR_API_KEY=OLD, **{self.purpose_env: self.new_value}):
            self.assertEqual(self.check("otra-llave"), 401)
            self.assertEqual(self.check(OLD + "x"), 401)
            self.assertEqual(self.check(self.new_value[:-1]), 401)

    def test_llave_vacia_o_ausente_no_autentica(self):
        with _env(LIQUIDADOR_API_KEY=OLD, **{self.purpose_env: self.new_value}):
            self.assertEqual(self.check(""), 401)
            self.assertEqual(self.check(None), 401)
            self.assertEqual(self.check("   "), 401)

    def test_sin_llaves_configuradas_falla_cerrado(self):
        with _env():
            self.assertEqual(self.check("cualquiera"), 503)
            self.assertEqual(self.check(""), 503)
            self.assertEqual(self.check(None), 503)

    def test_variables_en_blanco_no_abren_con_cabecera_vacia(self):
        with _env(LIQUIDADOR_API_KEY="   ", **{self.purpose_env: '""'}):
            self.assertEqual(self.check(""), 503)
            self.assertEqual(self.check(" "), 503)

    def test_retiro_de_llave_vieja_con_interruptor(self):
        with _env(
            LIQUIDADOR_API_KEY=OLD,
            M2M_LEGACY_KEY_ENABLED="false",
            **{self.purpose_env: self.new_value},
        ):
            self.assertEqual(self.check(OLD), 401)
            self.assertEqual(self.check(self.new_value), 200)

    def test_retiro_borrando_variable_vieja(self):
        with _env(**{self.purpose_env: self.new_value}):
            self.assertEqual(self.check(OLD), 401)
            self.assertEqual(self.check(self.new_value), 200)

    def test_longitudes_distintas_no_lanzan(self):
        with _env(**{self.purpose_env: self.new_value}):
            self.assertEqual(self.check("x"), 401)
            self.assertEqual(self.check("x" * 5000), 401)
            self.assertEqual(self.check("llave-ñ-no-ascii"), 401)


class BotApiKeyTests(ApiKeyMatrixMixin, unittest.TestCase):
    purpose_env = "BOT_API_KEY"
    new_value = NEW_BOT

    def check(self, request_key):
        return _status(bot_api._require_api_key, _req(request_key))

    def test_llave_de_recaudos_no_abre_bot(self):
        with _env(BOT_API_KEY=NEW_BOT, RECAUDOS_API_KEY=NEW_REC):
            self.assertEqual(self.check(NEW_REC), 401)


class RecaudosApiKeyTests(ApiKeyMatrixMixin, unittest.TestCase):
    purpose_env = "RECAUDOS_API_KEY"
    new_value = NEW_REC

    def check(self, request_key):
        return _status(api_recaudos._require_bot_auth, _req(request_key))

    def test_llave_de_bot_no_abre_recaudos(self):
        with _env(BOT_API_KEY=NEW_BOT, RECAUDOS_API_KEY=NEW_REC):
            self.assertEqual(self.check(NEW_BOT), 401)


class PdfSigningTests(unittest.TestCase):
    def _signed_query(self, secret, filename="Estado_Cuenta_1.pdf", delta=300):
        expires = int(time.time()) + delta
        payload = f"{filename}|{expires}".encode()
        token = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return filename, expires, token

    def _serve(self, filename, expires, token):
        with mock.patch("pdf_storage.resolve_pdf", side_effect=ValueError("no existe")):
            return _status(bot_api.servir_pdf_bot, filename, expires, token)

    def test_firma_con_nueva_si_existe(self):
        with _env(LIQUIDADOR_API_KEY=OLD, BOT_PDF_PUBLIC_SECRET=NEW_PDF):
            url = bot_api.build_signed_pdf_url("a.pdf", "https://erp.example")
            token = url.split("token=")[1]
            expires = int(url.split("expires=")[1].split("&")[0])
            self.assertEqual(token, bot_api._sign("a.pdf", expires, NEW_PDF))

    def test_firma_con_heredada_si_no_hay_nueva(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            url = bot_api.build_signed_pdf_url("a.pdf", "https://erp.example")
            expires = int(url.split("expires=")[1].split("&")[0])
            self.assertEqual(url.split("token=")[1], bot_api._sign("a.pdf", expires, OLD))

    def test_sin_secretos_no_firma_ni_sirve(self):
        with _env():
            with self.assertRaises(RuntimeError):
                bot_api.build_signed_pdf_url("a.pdf", "https://erp.example")
            self.assertEqual(self._serve(*self._signed_query(OLD)), 503)

    def test_solo_vieja_verifica_vieja(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self._serve(*self._signed_query(OLD)), 404)
            self.assertEqual(self._serve(*self._signed_query(NEW_PDF)), 403)

    def test_solo_nueva_verifica_nueva(self):
        with _env(BOT_PDF_PUBLIC_SECRET=NEW_PDF):
            self.assertEqual(self._serve(*self._signed_query(NEW_PDF)), 404)
            self.assertEqual(self._serve(*self._signed_query(OLD)), 403)

    def test_ambas_verifican_enlaces_en_vuelo(self):
        with _env(LIQUIDADOR_API_KEY=OLD, BOT_PDF_PUBLIC_SECRET=NEW_PDF):
            self.assertEqual(self._serve(*self._signed_query(OLD)), 404)
            self.assertEqual(self._serve(*self._signed_query(NEW_PDF)), 404)

    def test_token_incorrecto_o_vacio(self):
        with _env(LIQUIDADOR_API_KEY=OLD, BOT_PDF_PUBLIC_SECRET=NEW_PDF):
            filename, expires, _ = self._signed_query(NEW_PDF)
            self.assertEqual(self._serve(filename, expires, "deadbeef"), 403)
            self.assertEqual(self._serve(filename, expires, ""), 403)

    def test_firma_vacia_no_valida_con_secreto_vacio(self):
        with _env():
            filename, expires, token = self._signed_query("")
            self.assertEqual(self._serve(filename, expires, token), 503)

    def test_enlace_expirado(self):
        with _env(BOT_PDF_PUBLIC_SECRET=NEW_PDF):
            filename, expires, token = self._signed_query(NEW_PDF, delta=-60)
            self.assertEqual(self._serve(filename, expires, token), 410)


class SupervisionKeyTests(unittest.TestCase):
    def _sent_key(self):
        sent = {}

        def fake_request(method, url, headers=None, **kw):
            sent["key"] = headers.get("X-Agent-Supervision-Key")
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"status": "success"}
            return resp

        with mock.patch("agent_supervision.requests.request", side_effect=fake_request):
            data, code = agent_supervision._proxy("GET", "/control/x")
        return sent.get("key"), code, data

    def test_solo_vieja_mantiene_comportamiento(self):
        with _env(LIQUIDADOR_API_KEY=OLD):
            self.assertEqual(self._sent_key()[:2], (OLD, 200))

    def test_solo_nueva(self):
        with _env(AGENT_SUPERVISION_KEY=NEW_SUP):
            self.assertEqual(self._sent_key()[:2], (NEW_SUP, 200))

    def test_ambas_prefiere_nueva(self):
        with _env(LIQUIDADOR_API_KEY=OLD, AGENT_SUPERVISION_KEY=NEW_SUP):
            self.assertEqual(self._sent_key()[:2], (NEW_SUP, 200))

    def test_vacia_falla_cerrado_sin_llamar_al_agente(self):
        with _env(AGENT_SUPERVISION_KEY="  ", LIQUIDADOR_API_KEY=""):
            with mock.patch("agent_supervision.requests.request") as req:
                data, code = agent_supervision._proxy("GET", "/control/x")
            self.assertEqual(code, 503)
            req.assert_not_called()
            self.assertEqual(data["codigo"], "AGENT_SUPERVISION_KEY_MISSING")

    def test_retirada_vieja_sin_nueva_falla_cerrado(self):
        with _env(LIQUIDADOR_API_KEY=OLD, M2M_LEGACY_KEY_ENABLED="false"):
            with mock.patch("agent_supervision.requests.request") as req:
                _data, code = agent_supervision._proxy("GET", "/control/x")
            self.assertEqual(code, 503)
            req.assert_not_called()

    def test_retirada_vieja_con_nueva_funciona(self):
        with _env(
            LIQUIDADOR_API_KEY=OLD,
            M2M_LEGACY_KEY_ENABLED="false",
            AGENT_SUPERVISION_KEY=NEW_SUP,
        ):
            self.assertEqual(self._sent_key()[:2], (NEW_SUP, 200))


class NoSecretLoggingTests(unittest.TestCase):
    def test_logs_no_contienen_valores(self):
        m2m_keys._legacy_last_log.clear()
        with _env(LIQUIDADOR_API_KEY=OLD, BOT_API_KEY=NEW_BOT), mock.patch(
            "builtins.print"
        ) as fake_print:
            m2m_keys.log_config_summary()
            bot_api._require_api_key(_req(OLD))
            with self.assertRaises(HTTPException):
                bot_api._require_api_key(_req("incorrecta-secreta"))
        salida = " ".join(str(c) for c in fake_print.call_args_list)
        for secreto in (OLD, NEW_BOT, "incorrecta-secreta"):
            self.assertNotIn(secreto, salida)
        self.assertIn("heredada", salida)

    def test_describe_config(self):
        with _env(LIQUIDADOR_API_KEY=OLD, BOT_API_KEY=NEW_BOT):
            estado = m2m_keys.describe_config()
        self.assertEqual(estado["bot"], "nueva")
        self.assertEqual(estado["recaudos"], "solo-heredada")
        with _env():
            self.assertEqual(set(m2m_keys.describe_config().values()), {"faltante"})


if __name__ == "__main__":
    unittest.main()
