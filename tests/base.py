"""Общая основа тестов: свежий стенд (учебные сервисы + интеграция) на каждый тест, короткие паузы."""
from __future__ import annotations

import json
import time
import unittest
from typing import Tuple

from mocks.pay import sign
from stand import SECRET, Stand


class StandCase(unittest.TestCase):
    stand_kw: dict = {}

    def setUp(self):
        self.st = Stand(delays=(0, 0.01, 0.02, 0.03), backoff=0.01, poll_after_s=0, **self.stand_kw)
        self.addCleanup(self.st.stop)

    # ------------------------------------------------------------------ помощники
    def place(self, lead_id: str = 'lead-1', product: str = 'excel-mini', name: str = 'Анна Ковалёва',
              email: str = 'anna@example.com') -> Tuple[str, str]:
        """Оформляет заказ, возвращает (номер заказа, ссылка на оплату)."""
        r = self.st.submit(lead_id, name, email, product)
        self.assertIn(r.status_code, (200, 201, 202), r.text)
        return r.json()['order_id'], r.json()['pay_url']

    def order(self, ref: str) -> dict:
        return self.st.svc.store.order(ref)

    def deal_of(self, ref: str) -> dict:
        return self.st.deal(self.order(ref)['crm_deal_id'])

    def texts(self, ref: str) -> list:
        return [c['text'].replace(' ', ' ') for c in self.deal_of(ref)['timeline']]

    def mails(self, email: str) -> list:
        return [m for m in self.st.messages() if m['to'] == email]

    def event(self, ref: str, **over) -> dict:
        o = self.order(ref)
        data = {'invoice_id': o['invoice_id'], 'payment_id': 'pay_x1', 'order_ref': ref, 'amount_kop': o['amount_kop'],
                'currency': 'RUB', 'receipt': {'number': 'DEMO-X1'}}
        return {'id': over.pop('id', 'evt_x1'), 'type': over.pop('type', 'payment.succeeded'),
                'created_at': int(time.time()), 'data': {**data, **over}}

    def signed(self, event, ts=None, secret=SECRET):
        raw = event if isinstance(event, bytes) else json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode()
        return raw, sign(secret, raw, int(time.time()) if ts is None else ts)

    def send(self, event, **kw):
        raw, sig = self.signed(event, **kw)
        return self.st.post_webhook(raw, sig)
