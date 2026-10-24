"""Запуск Flask-приложения в фоновом потоке на свободном порту (для мок-сервисов, интеграции и тестов)."""
from __future__ import annotations

import logging
import os
import threading

from werkzeug.serving import make_server

logging.getLogger('werkzeug').setLevel(logging.ERROR)      # без строки на каждый запрос


def local_direct() -> None:
    """Запросы к 127.0.0.1 не должны идти через корпоративный прокси из переменных окружения."""
    for name in ('NO_PROXY', 'no_proxy'):
        have = [h for h in os.environ.get(name, '').split(',') if h]
        os.environ[name] = ','.join(have + [h for h in ('127.0.0.1', 'localhost') if h not in have])


class ServerThread:
    def __init__(self, app, host: str = '127.0.0.1', port: int = 0):
        local_direct()
        self._srv = make_server(host, port, app, threaded=True)
        self.port = self._srv.server_port
        self.url = f'http://{host}:{self.port}'
        self._thread = threading.Thread(target=lambda: self._srv.serve_forever(poll_interval=0.02), daemon=True)

    def start(self) -> 'ServerThread':
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._thread.join(timeout=5)
