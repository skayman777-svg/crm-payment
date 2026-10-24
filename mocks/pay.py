"""Учебный платёжный провайдер: счета, страница оплаты, подписанные вебхуки с повторной доставкой.

Не настоящая платёжная система и не повторяет API конкретного сервиса. Подпись вебхука здесь реализована
независимо от кода интеграции (так проверяется совместимость формата, а не копия одной функции):
заголовок `X-Demo-Signature: t=<unix-время>,v1=<HMAC-SHA256(секрет, "<t>.<тело>")>`.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
import threading
import time
from html import escape
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, redirect, request

from ._ui import page, rub

log = logging.getLogger('pay')


def sign(secret: str, raw: bytes, ts: int) -> str:
    mac = hmac.new(secret.encode(), str(ts).encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return f't={ts},v1={mac}'


class Provider:
    def __init__(self, api_key: str = 'pay-demo-key', webhook_url: str = '', webhook_secret: str = 'whsec-demo',
                 delays: Tuple[float, ...] = (0, 0.3, 0.6, 1.2), merchant: str = 'Школа «Excel для учёта»'):
        self.api_key, self.webhook_url, self.secret = api_key, webhook_url, webhook_secret
        self.delays, self.merchant = delays, merchant
        self.base_url = ''
        self.invoices: Dict[str, dict] = {}
        self.idem: Dict[str, Tuple[str, dict]] = {}
        self.events: List[dict] = []
        self.fail = 0                                  # сколько ближайших запросов к /v1 ответят 503
        self.last_codes: List[int] = []                # итог последней доставки вебхука (для проверок в прогоне и тестах)
        self.last_body = ''
        self._inv_seq, self._pay_seq, self._evt_seq = itertools.count(1), itertools.count(1), itertools.count(1)
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- счета
    def create_invoice(self, key: str, body: dict) -> Tuple[int, dict]:
        fp = json.dumps(body, sort_keys=True, ensure_ascii=False)
        with self.lock:
            if key in self.idem:
                old_fp, resp = self.idem[key]
                if old_fp != fp:
                    log.warning('POST /v1/invoices key=%s → 422: ключ уже использован с другим телом запроса', key)
                    return 422, {'error': 'idempotence key reused with a different request body'}
                log.info('POST /v1/invoices key=%s → 200: счёт %s уже создан, дубль не создан', key, resp['id'])
                return 200, resp
            amount = body.get('amount_kop')
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or body.get('currency') != 'RUB':
                return 422, {'error': 'amount_kop must be a positive integer and currency must be RUB'}
            iid = f'inv_{next(self._inv_seq):04d}'
            inv = {'id': iid, 'status': 'pending', 'order_ref': body.get('order_ref'), 'amount_kop': amount,
                   'currency': 'RUB', 'description': body.get('description', ''), 'customer': body.get('customer', {}),
                   'items': body.get('items', []), 'pay_url': f'{self.base_url}/pay/{iid}', 'attempts': []}
            self.invoices[iid] = inv
            self.idem[key] = (fp, inv)
        log.info('POST /v1/invoices key=%s → 201: счёт %s на %s', key, iid, rub(amount))
        return 201, inv

    def public(self, inv: dict) -> dict:
        return {k: v for k, v in inv.items() if k != 'attempts'}

    # ---------------------------------------------------------------- платёж и вебхук
    def complete(self, invoice_id: str, outcome: str = 'success', *, deliver: bool = True,
                 reason: str = 'insufficient_funds') -> Tuple[dict, List[int]]:
        """Клиент нажал «Оплатить» или «Отклонить» на странице оплаты. Возвращает событие и коды ответов на доставку."""
        with self.lock:
            inv = self.invoices[invoice_id]
            pid = f'pay_{next(self._pay_seq):04d}'
            data = {'invoice_id': invoice_id, 'payment_id': pid, 'order_ref': inv['order_ref'],
                    'amount_kop': inv['amount_kop'], 'currency': inv['currency']}
            if outcome == 'success':
                inv['status'] = 'paid'
                inv['payment'] = {'id': pid, 'receipt': {'number': f'DEMO-{pid[4:].zfill(6)}', 'fiscal': False,
                                                         'note': 'учебный чек, не фискальный'}}
                data['receipt'] = inv['payment']['receipt']
                etype, verdict = 'payment.succeeded', 'прошёл'
            else:
                inv['attempts'].append({'id': pid, 'reason': reason})
                data['reason'] = reason
                etype, verdict = 'payment.failed', f'отклонён ({reason})'
            event = {'id': f'evt_{next(self._evt_seq):04d}', 'type': etype, 'created_at': int(time.time()), 'data': data}
            self.events.append(event)
        log.info('платёж %s по счёту %s %s', pid, invoice_id, verdict)
        codes = self.deliver(event) if deliver else []
        if not deliver:
            log.warning('вебхук %s %s не отправлен (имитация потерянного уведомления)', event['id'], etype)
        return event, codes

    def deliver(self, event: dict, *, secret: Optional[str] = None, ts: Optional[int] = None,
                raw: Optional[bytes] = None, retry: bool = True) -> List[int]:
        """Отправляет вебхук; при ответе не 2xx или ошибке сети повторяет с нарастающей паузой (как делают провайдеры)."""
        raw = raw if raw is not None else json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode()
        delays = self.delays if retry else self.delays[:1]
        codes: List[int] = []
        self.last_codes = codes
        for n, wait in enumerate(delays, 1):
            time.sleep(wait)
            sig = sign(secret or self.secret, raw, int(time.time()) if ts is None else ts)
            try:
                resp = requests.post(self.webhook_url, data=raw, timeout=5,
                                     headers={'Content-Type': 'application/json', 'X-Demo-Signature': sig})
                code, self.last_body = resp.status_code, resp.text
            except requests.RequestException as exc:
                code = 0
                log.warning('вебхук %s: попытка %d → нет соединения (%s)', event['id'], n, type(exc).__name__)
            codes.append(code)
            if 200 <= code < 300:
                log.info('вебхук %s доставлен с попытки %d (HTTP %d)', event['id'], n, code)
                return codes
            if code:
                log.warning('вебхук %s: попытка %d → HTTP %d%s', event['id'], n, code,
                            f', повтор через {delays[n]:g} с' if n < len(delays) else '')
        log.warning('вебхук %s: ответа 2xx нет (попыток: %d, коды: %s)', event['id'], len(codes), codes)
        return codes


def create_app(provider: Provider) -> Flask:
    app = Flask('pay-mock')
    app.json.ensure_ascii = False

    def guard():
        with provider.lock:
            if provider.fail > 0:
                provider.fail -= 1
                log.warning('%s %s → 503 (имитация сбоя)', request.method, request.path)
                return jsonify(error='temporarily unavailable'), 503
        if request.headers.get('Authorization') != f'Bearer {provider.api_key}':
            log.warning('%s %s → 401 (неверный ключ)', request.method, request.path)
            return jsonify(error='unauthorized'), 401
        return None

    @app.post('/v1/invoices')
    def create_invoice():
        denied = guard()
        if denied:
            return denied
        key = request.headers.get('Idempotence-Key')
        if not key:
            return jsonify(error='Idempotence-Key header is required'), 400
        code, resp = provider.create_invoice(key, request.get_json(silent=True) or {})
        return jsonify(provider.public(resp) if code < 300 else resp), code

    @app.get('/v1/invoices/<iid>')
    def get_invoice(iid):
        denied = guard()
        if denied:
            return denied
        inv = provider.invoices.get(iid)
        if not inv:
            return jsonify(error='not found'), 404
        log.info('GET /v1/invoices/%s → 200: статус %s', iid, inv['status'])
        return jsonify(provider.public(inv)), 200

    @app.get('/pay/<iid>')
    def checkout(iid):
        inv = provider.invoices.get(iid)
        if not inv:
            return 'not found', 404
        return page(f'Оплата {iid}', _checkout(provider, inv), 'ТЕСТОВЫЙ СТЕНД: учебная страница оплаты, деньги не списываются',
                    PAY_CSS)

    @app.post('/pay/<iid>')
    def checkout_submit(iid):
        inv = provider.invoices.get(iid)
        if not inv:
            return 'not found', 404
        if inv['status'] == 'pending':
            provider.complete(iid, 'success' if request.form.get('outcome') == 'success' else 'fail')
        return redirect(f'/pay/{iid}', code=303)

    app.provider = provider
    return app


PAY_CSS = """
.pay{max-width:460px;margin:20px auto}.pay h1{font-size:20px}.row{display:flex;justify-content:space-between;gap:12px;
padding:9px 0;border-bottom:1px solid var(--line)}.row:last-of-type{border:0}
.total{font-size:26px;font-weight:700;margin:14px 0 4px}.ok{color:#215c3a;font-weight:700}
.acts{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.hint{font-size:13px;color:var(--muted);margin-top:14px}
"""


def _checkout(provider: Provider, inv: dict) -> str:
    rows = (f'<div class="row"><span class="muted">Получатель</span><span>{escape(provider.merchant)}</span></div>'
            f'<div class="row"><span class="muted">Заказ</span><span>{escape(str(inv["order_ref"]))}</span></div>'
            f'<div class="row"><span class="muted">Счёт</span><span>{escape(inv["id"])}</span></div>'
            f'<div class="row"><span class="muted">За что</span><span>{escape(inv["description"])}</span></div>')
    if inv['status'] == 'paid':
        rc = inv['payment']['receipt']
        tail = (f'<p class="ok">✓ Счёт оплачен</p><p class="muted">Учебный чек № {escape(rc["number"])} '
                f'(не фискальный). Клиент получит письмо с доступом.</p>')
    else:
        failed = ('<p style="color:#b83a2b;font-weight:600">Прошлая попытка отклонена. Можно повторить.</p>'
                  if inv['attempts'] else '')
        tail = (f'{failed}<form method="post" class="acts"><button class="btn" name="outcome" value="success">Оплатить</button>'
                f'<button class="btn alt" name="outcome" value="fail">Отклонить платёж</button></form>'
                '<p class="hint">Карту вводить не нужно: эта страница имитирует результат оплаты.</p>')
    return (f'<div class="pay card"><h1>Оплата заказа</h1>{rows}'
            f'<div class="total num">{rub(inv["amount_kop"])}</div>{tail}</div>')
