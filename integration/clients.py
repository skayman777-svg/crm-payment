"""Тонкие обёртки над API внешних систем (CRM, платёжный сервис, почта и Telegram).

Под реальные системы правятся запросы и разбор ответов здесь, а формат подписи и событий вебхука у каждого
провайдера свой: он в signature.py и Service.handle_webhook. Методы возвращают то, что ждёт service.py
(см. README, раздел «Как подключить свои системы»).
"""
from __future__ import annotations

from typing import Optional

from .http import Api


class CRM:
    def __init__(self, api: Api):
        self.api = api

    def create_deal(self, external_id: str, title: str, amount_kop: int, contact: dict) -> dict:
        """Идемпотентно: повтор с тем же external_id вернёт ту же сделку."""
        body = {'external_id': external_id, 'title': title, 'amount_kop': amount_kop, 'contact': contact}
        return self.api.request('POST', '/api/deals', json=body).json()

    def get_deal(self, deal_id: str) -> dict:
        return self.api.request('GET', f'/api/deals/{deal_id}').json()

    def set_stage(self, deal_id: str, stage: str, fields: Optional[dict] = None) -> dict:
        body: dict = {'stage': stage}
        if fields:
            body['fields'] = fields
        return self.api.request('PATCH', f'/api/deals/{deal_id}', json=body).json()

    def comment(self, deal_id: str, uid: str, text: str) -> None:
        """uid делает отправку безопасной для повтора: тот же комментарий второй раз не появится."""
        self.api.request('POST', f'/api/deals/{deal_id}/comments', json={'uid': uid, 'text': text})


class Pay:
    def __init__(self, api: Api):
        self.api = api

    def create_invoice(self, order_ref: str, amount_kop: int, description: str, email: str) -> dict:
        """Ключ идемпотентности = номер заказа: повтор запроса не выставит второй счёт."""
        body = {'order_ref': order_ref, 'amount_kop': amount_kop, 'currency': 'RUB', 'description': description,
                'customer': {'email': email},
                'items': [{'name': description, 'qty': 1, 'price_kop': amount_kop, 'vat': 'none'}]}
        return self.api.request('POST', '/v1/invoices', json=body, headers={'Idempotence-Key': order_ref}).json()

    def get_invoice(self, invoice_id: str) -> dict:
        return self.api.request('GET', f'/v1/invoices/{invoice_id}').json()


class Notifier:
    def __init__(self, api: Api):
        self.api = api

    def send(self, channel: str, to: str, text: str) -> None:
        self.api.request('POST', '/send', json={'channel': channel, 'to': to, 'text': text})
