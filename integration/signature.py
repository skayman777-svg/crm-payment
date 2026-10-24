"""Подпись вебхуков: HMAC-SHA256 от «<время>.<тело запроса>» и защита от повторной отправки старого запроса."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

HEADER = 'X-Demo-Signature'


class SignatureError(Exception):
    """Подпись не принята. Причина нужна для журнала; наружу (провайдеру) она не отдаётся."""


def _mac(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(secret.encode(), str(ts).encode() + b'.' + body, hashlib.sha256).hexdigest()


def sign(secret: str, body: bytes, ts: Optional[int] = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return f't={ts},v1={_mac(secret, ts, body)}'


def verify(secret: str, body: bytes, header: Optional[str], tolerance: int = 300, now: Optional[float] = None) -> int:
    """Проверяет подпись по сырому телу запроса (до разбора JSON). Возвращает метку времени или бросает SignatureError."""
    if not header:
        raise SignatureError('заголовок подписи отсутствует')
    try:
        parts = dict(p.strip().split('=', 1) for p in header.split(','))
        ts, received = int(parts['t']), parts['v1']
    except (KeyError, ValueError):
        raise SignatureError('заголовок подписи в неверном формате') from None
    if not hmac.compare_digest(_mac(secret, ts, body).encode(), received.encode()):      # сравнение за постоянное время
        raise SignatureError('подпись не совпала')
    if abs((time.time() if now is None else now) - ts) > tolerance:
        raise SignatureError(f'метка времени старше {tolerance} с: возможна повторная отправка перехваченного запроса')
    return ts
