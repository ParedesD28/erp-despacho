"""Límites de peticiones e intentos de login (en memoria, instancia única).

Almacenamiento: diccionarios en memoria del proceso, protegidos con un lock.
Es suficiente para una sola instancia de Render. Con N instancias cada una
lleva su propio contador (el límite efectivo se multiplica por N); en ese caso
habría que mover el estado a una tabla de Neon. Al reiniciar el servicio los
contadores y bloqueos se reinician.

IP del cliente: Render antepone Cloudflare y su balanceador. `X-Forwarded-For`
conserva lo que envíe el cliente (se le añade, no se reemplaza), así que su
extremo izquierdo es falsificable. Solo se confía en:
  1. `CF-Connecting-IP` (Cloudflare lo fija y rechaza el que envíe el cliente),
  2. `X-Forwarded-For` contando desde la derecha `ERP_TRUSTED_PROXY_COUNT`
     proxies de confianza (por defecto 0 = desactivado),
y solo si la conexión directa proviene de una IP privada/loopback (el proxy de
Render). Si nada aplica se usa la IP de la conexión.
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# --- Configuración (ajustable por entorno) ---------------------------------
GLOBAL_LIMIT = _env_int("RATE_LIMIT_GLOBAL_PER_MIN", 600)
API_KEY_LIMIT = _env_int("RATE_LIMIT_API_PER_MIN", 240)
API_FAIL_LIMIT = _env_int("RATE_LIMIT_API_FAILS", 10)
LOGIN_IP_FAILS = _env_int("RATE_LIMIT_LOGIN_IP_FAILS", 20)
LOGIN_USER_FAILS = _env_int("RATE_LIMIT_LOGIN_USER_FAILS", 10)
LOGIN_PAIR_FAILS = _env_int("RATE_LIMIT_LOGIN_PAIR_FAILS", 5)
LOGIN_WINDOW = _env_int("RATE_LIMIT_LOGIN_WINDOW_SECONDS", 900)
LOCK_BASE = _env_int("RATE_LIMIT_LOCK_BASE_SECONDS", 60)
LOCK_MAX = _env_int("RATE_LIMIT_LOCK_MAX_SECONDS", 900)
STRIKE_MEMORY = 3600
MAX_KEYS = 20000

API_KEY_PREFIXES = ("/api/bot/", "/api/recaudos/", "/sms/api/", "/api/v1/cron/")
UNTHROTTLED_PREFIXES = ("/static/",)
UNTHROTTLED_PATHS = frozenset({"/health"})


# --- IP del cliente ----------------------------------------------------------
def _parse_ip(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return None


def _is_private(value: Optional[str]) -> bool:
    ip = _parse_ip(value)
    if not ip:
        return False
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local


def client_ip(headers, peer: Optional[str], trusted_proxies: Optional[int] = None) -> str:
    """IP del cliente sin confiar en valores controlables por el atacante."""
    peer_ip = _parse_ip(peer) or (peer or "desconocida")
    if not _is_private(peer):
        return peer_ip
    cf = _parse_ip(headers.get("cf-connecting-ip"))
    if cf:
        return cf
    if trusted_proxies is None:
        try:
            hops = int(os.getenv("ERP_TRUSTED_PROXY_COUNT", "0"))
        except ValueError:
            hops = 0
    else:
        hops = trusted_proxies
    if hops > 0:
        partes = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
        if len(partes) >= hops:
            candidato = _parse_ip(partes[-hops])
            if candidato:
                return candidato
    return peer_ip


# --- Ventana deslizante -------------------------------------------------------
class SlidingWindow:
    """Cuenta eventos por clave en una ventana de `window` segundos."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        if len(self._hits) < MAX_KEYS:
            return
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            self._hits.pop(key, None)
        if len(self._hits) >= MAX_KEYS:
            self._hits.clear()

    def hit(self, key: str) -> tuple[bool, int]:
        """Registra un evento. Devuelve (permitido, segundos_para_reintentar)."""
        now = self.clock()
        with self._lock:
            self._purge(now)
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                retry = int(q[0] + self.window - now) + 1
                return False, max(1, retry)
            q.append(now)
            return True, 0


# --- Fallos con bloqueo temporal y retroceso exponencial ---------------------
@dataclass
class _Estado:
    fails: Deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    strikes: int = 0
    last: float = 0.0


class FailureLimiter:
    """Tras `limit` fallos dentro de `window` bloquea `base * 2^(strikes-1)` s (tope `max_lock`)."""

    def __init__(
        self,
        limit: int,
        window: float = LOGIN_WINDOW,
        base: float = LOCK_BASE,
        max_lock: float = LOCK_MAX,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.base = base
        self.max_lock = max_lock
        self.clock = clock
        self._data: Dict[str, _Estado] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        if len(self._data) < MAX_KEYS:
            return
        for key in [
            k for k, e in self._data.items()
            if e.locked_until <= now and now - e.last > max(self.window, STRIKE_MEMORY)
        ]:
            self._data.pop(key, None)
        if len(self._data) >= MAX_KEYS:
            self._data.clear()

    def blocked_for(self, key: str) -> int:
        now = self.clock()
        with self._lock:
            e = self._data.get(key)
            if e and e.locked_until > now:
                return int(e.locked_until - now) + 1
            return 0

    def failure(self, key: str) -> int:
        """Registra un fallo; devuelve segundos de bloqueo si se activó (0 si no)."""
        now = self.clock()
        with self._lock:
            self._purge(now)
            e = self._data.setdefault(key, _Estado())
            if e.last and now - e.last > STRIKE_MEMORY:
                e.strikes = 0
            e.last = now
            while e.fails and e.fails[0] <= now - self.window:
                e.fails.popleft()
            e.fails.append(now)
            if len(e.fails) >= self.limit:
                e.strikes += 1
                duracion = min(self.base * (2 ** (e.strikes - 1)), self.max_lock)
                e.locked_until = now + duracion
                e.fails.clear()
                return int(duracion)
            return 0

    def success(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


class AccessLimiter:
    """Agrupa el throttle global, el de API keys y los fallos de login."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.global_window = SlidingWindow(GLOBAL_LIMIT, 60, clock)
        self.api_window = SlidingWindow(API_KEY_LIMIT, 60, clock)
        self.api_failures = FailureLimiter(API_FAIL_LIMIT, 300, LOCK_BASE, LOCK_MAX, clock)
        self.login_ip = FailureLimiter(LOGIN_IP_FAILS, clock=clock)
        self.login_user = FailureLimiter(LOGIN_USER_FAILS, clock=clock)
        self.login_pair = FailureLimiter(LOGIN_PAIR_FAILS, clock=clock)

    @staticmethod
    def _norm_user(email: str) -> str:
        return (email or "").strip().lower()[:254]

    def check_request(self, ip: str, path: str) -> int:
        """Segundos a esperar si la petición debe rechazarse con 429; 0 si pasa."""
        if path in UNTHROTTLED_PATHS or path.startswith(UNTHROTTLED_PREFIXES):
            return 0
        if path.startswith(API_KEY_PREFIXES):
            espera = self.api_failures.blocked_for(ip)
            if espera:
                return espera
            ok, retry = self.api_window.hit(ip)
            if not ok:
                return retry
        ok, retry = self.global_window.hit(ip)
        return 0 if ok else retry

    def record_api_response(self, ip: str, path: str, status_code: int) -> None:
        if path.startswith(API_KEY_PREFIXES) and status_code in (401, 403):
            self.api_failures.failure(ip)

    def login_blocked_for(self, ip: str, email: str) -> int:
        user = self._norm_user(email)
        return max(
            self.login_ip.blocked_for(ip),
            self.login_user.blocked_for(user),
            self.login_pair.blocked_for(f"{ip}|{user}"),
        )

    def login_failure(self, ip: str, email: str) -> None:
        user = self._norm_user(email)
        self.login_ip.failure(ip)
        if user:
            self.login_user.failure(user)
            self.login_pair.failure(f"{ip}|{user}")

    def login_success(self, ip: str, email: str) -> None:
        user = self._norm_user(email)
        self.login_user.success(user)
        self.login_pair.success(f"{ip}|{user}")

    def reset(self) -> None:
        for lim in (self.api_failures, self.login_ip, self.login_user, self.login_pair):
            lim.reset()
        self.global_window = SlidingWindow(GLOBAL_LIMIT, 60, self.clock)
        self.api_window = SlidingWindow(API_KEY_LIMIT, 60, self.clock)


limiter = AccessLimiter()
