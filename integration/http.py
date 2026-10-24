"""HTTP-клиент для внешних систем: таймаут, повтор при сбоях (5xx, 429, сеть) с нарастающей паузой, понятные ошибки."""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional

import requests

log = logging.getLogger('svc')
RETRY_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    """Система ответила отказом (4xx). Повтор не поможет: нужно менять запрос или данные."""

    def __init__(self, system: str, method: str, path: str, status: int, text: str):
        self.system, self.status = system, status
        super().__init__(f'{system}: {method} {path} → HTTP {status}: {text[:120]}')


class Unavailable(Exception):
    """Система не отвечает или отвечает ошибкой сервера даже после повторов. Шаг откладывается, а не теряется."""

    def __init__(self, system: str, message: str):
        self.system = system
        super().__init__(message)


class Api:
    def __init__(self, system: str, base_url: str, headers: Optional[dict] = None, *, attempts: int = 3,
                 backoff: float = 0.2, jitter: bool = True, timeout: float = 3.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.system, self.base, self.attempts = system, base_url.rstrip('/'), attempts
        self.backoff, self.jitter, self.timeout, self.sleep = backoff, jitter, timeout, sleep
        self.session = requests.Session()
        self.session.headers.update(headers or {})

    def request(self, method: str, path: str, **kw) -> requests.Response:
        for n in range(1, self.attempts + 1):
            try:
                resp = self.session.request(method, self.base + path, timeout=self.timeout, **kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                reason = f'нет соединения ({type(exc).__name__})'
            else:
                if resp.status_code not in RETRY_STATUS:
                    if resp.status_code >= 400:
                        raise ApiError(self.system, method, path, resp.status_code, resp.text)
                    return resp
                reason = f'ответ {resp.status_code}'
            if n == self.attempts:
                raise Unavailable(self.system, f'{self.system} недоступна: {method} {path}, {reason}, попыток: {n}')
            pause = self.backoff * 2 ** (n - 1)
            if self.jitter:
                pause *= random.uniform(0.75, 1.25)
            log.warning('%s: %s %s — %s, повтор %d из %d через %.1f с', self.system, method, path, reason,
                        n, self.attempts - 1, pause)
            self.sleep(pause)
        raise AssertionError('unreachable')
