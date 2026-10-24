"""Локальный стенд: тестовые CRM, платёжный сервис, почта и сайт + сервис интеграции. Для run.py и тестов."""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from integration.config import CATALOG, Settings
from integration.service import Service
from integration.web import create_app
from mocks import crm, messenger, pay, site
from mocks._server import ServerThread

gate_log = logging.getLogger('gate')
CRM_TOKEN, PAY_KEY, SECRET, LEAD_TOKEN = 'crm-demo-token', 'pay-demo-key', 'whsec-demo', 'site-demo-token'


class FlakyGate:
    """Имитация шлюза перед сервисом: первые N запросов к вебхуку получают 502 (перезапуск, деплой)."""

    def __init__(self, app):
        self.app, self.fail = app, 0

    def __call__(self, environ, start_response):
        if self.fail > 0 and environ.get('PATH_INFO') == '/webhooks/pay':
            self.fail -= 1
            gate_log.warning('POST /webhooks/pay → 502 (имитация: сервис перезапускается)')
            start_response('502 Bad Gateway', [('Content-Type', 'text/plain')])
            return [b'bad gateway']
        return self.app(environ, start_response)


class Stand:
    def __init__(self, *, db_path: str = ':memory:', delays=(0, 0.3, 0.6, 1.2), backoff: float = 0.2,
                 poll_after_s: int = 0, step_max_attempts: int = 8):
        self.crm_app = crm.create_app(CRM_TOKEN)
        self.msg_app = messenger.create_app()
        self.provider = pay.Provider(api_key=PAY_KEY, webhook_secret=SECRET, delays=delays)
        self.servers: List[ServerThread] = []
        self.crm_srv = self._start(self.crm_app)
        self.msg_srv = self._start(self.msg_app)
        self.pay_srv = self._start(pay.create_app(self.provider))
        self.provider.base_url = self.pay_srv.url
        self.cfg = Settings(crm_url=self.crm_srv.url, crm_token=CRM_TOKEN, pay_url=self.pay_srv.url, pay_key=PAY_KEY,
                            webhook_secret=SECRET, msg_url=self.msg_srv.url, lead_token=LEAD_TOKEN, db_path=db_path,
                            http_backoff=backoff, http_jitter=False, poll_after_s=poll_after_s,
                            step_max_attempts=step_max_attempts)
        self.svc = Service(self.cfg)
        self.gate = FlakyGate(create_app(self.svc))
        self.svc_srv = self._start(self.gate)
        self.provider.webhook_url = f'{self.svc_srv.url}/webhooks/pay'
        offers = {c: (p.title, p.price_kop) for c, p in CATALOG.items()}
        self.site_srv = self._start(site.create_app(self.svc_srv.url, LEAD_TOKEN, offers))

    def _start(self, app) -> ServerThread:
        srv = ServerThread(app).start()
        self.servers.append(srv)
        return srv

    def stop(self) -> None:
        for srv in reversed(self.servers):
            srv.stop()
        self.svc.store.close()

    # ---------------------------------------------------------------- действия «снаружи»
    def submit(self, lead_id: str, name: str, email: str, product: str, **extra) -> requests.Response:
        """Заявка с сайта (как это делает обработчик формы)."""
        body = {'lead_id': lead_id, 'name': name, 'email': email, 'product': product, **extra}
        return requests.post(f'{self.svc_srv.url}/leads', json=body, timeout=10,
                             headers={'Authorization': f'Bearer {LEAD_TOKEN}'})

    def post_webhook(self, raw: bytes, signature: Optional[str]) -> requests.Response:
        headers = {'Content-Type': 'application/json'}
        if signature is not None:
            headers['X-Demo-Signature'] = signature
        return requests.post(f'{self.svc_srv.url}/webhooks/pay', data=raw, headers=headers, timeout=10)

    # ---------------------------------------------------------------- проверки «изнутри»
    def deal(self, deal_id: str) -> dict:
        return requests.get(f'{self.crm_srv.url}/api/deals/{deal_id}', headers={'Authorization': f'Bearer {CRM_TOKEN}'},
                            timeout=10).json()

    def deals(self) -> List[dict]:
        return list(self.crm_app.crm_state.deals.values())

    def messages(self) -> List[dict]:
        return list(self.msg_app.msg_state['messages'])

    def invoice_id(self, ref: str) -> str:
        return self.svc.store.order(ref)['invoice_id']

    def crm_down(self, down: bool) -> None:
        self.crm_app.crm_state.fail = 10 ** 6 if down else 0
