"""Запуск: python -m integration serve | reconcile. Настройки только из переменных окружения (см. README)."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from .config import Settings
from .service import Service
from .web import create_app


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m integration', description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    serve = sub.add_parser('serve', help='принимать заявки с сайта и вебхуки платёжного сервиса')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    sub.add_parser('reconcile', help='сверка со счетами провайдера и повтор отложенных шагов (запускать по расписанию)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(name)-4s %(message)s', datefmt='%H:%M:%S')
    svc = Service(Settings.from_env())
    try:
        if args.cmd == 'serve':
            # встроенный сервер Flask подходит для проверки; в работе ставят waitress или gunicorn за HTTPS-прокси
            create_app(svc).run(host=args.host, port=args.port, threaded=True)
            return 0
        res = svc.reconcile()
        logging.getLogger('svc').info('сверка завершена: проверено счетов %d, найдено оплат %d, шагов выполнено %d, '
                                      'ждут %d, требуют ручной проверки %d', res['polled'], res['recovered'], res['done'],
                                      res['pending'], res['dead'])
        return 1 if res['dead'] else 0              # ненулевой код удобно привязать к уведомлению планировщика
    finally:
        svc.store.close()


if __name__ == '__main__':
    sys.exit(main())
