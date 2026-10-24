"""Учебный сайт с формой заказа. Форма отправляется на сервер сайта, а он обращается к сервису интеграции:
так работает обработчик формы на реальном сайте (ключ доступа к сервису не попадает в браузер)."""
from __future__ import annotations

import uuid
from html import escape
from typing import Dict

import requests
from flask import Flask, redirect, request

from ._ui import page, rub

RIBBON = 'ТЕСТОВЫЙ СТЕНД: учебный сайт школы «Excel для учёта». Школа вымышленная, оплата не настоящая'


def create_app(service_url: str, lead_token: str, offers: Dict[str, tuple]) -> Flask:
    """offers: код продукта -> (название, цена в копейках). Цены на странице только показываются, платит клиент по цене сервера."""
    app = Flask('site-mock')

    @app.get('/')
    def index():
        cards = ''.join(
            f'<label class="offer"><input type="radio" name="product" value="{escape(code)}"'
            f'{" checked" if i == 0 else ""}><span><b>{escape(title)}</b><br>'
            f'<span class="muted">{rub(price)}</span></span></label>' for i, (code, (title, price)) in enumerate(offers.items()))
        body = ('<div class="shop"><h1>Школа «Excel для учёта»</h1><p class="sub">Выберите программу и оставьте заявку: '
                'ссылка на оплату откроется сразу, доступ придёт на почту после оплаты.</p>'
                '<form method="post" action="/order" class="card">'
                f'<input type="hidden" name="lead_id" value="web-{uuid.uuid4().hex[:10]}">'
                f'<div class="offers">{cards}</div>'
                '<label>Имя</label><input name="name" required maxlength="100" autocomplete="name" placeholder="Как к вам обращаться">'
                '<label>Почта</label><input name="email" type="email" required autocomplete="email" placeholder="name@example.com">'
                '<p><button class="btn" type="submit">Оформить заказ</button></p></form></div>')
        return page('Школа «Excel для учёта»', body, RIBBON, SHOP_CSS)

    @app.post('/order')
    def order():
        form = request.form
        payload = {k: form.get(k, '') for k in ('lead_id', 'name', 'email', 'product')}
        try:
            r = requests.post(f'{service_url}/leads', json=payload, timeout=10,
                              headers={'Authorization': f'Bearer {lead_token}'})
        except requests.RequestException:
            return _message('Сервис заказов временно недоступен', 'Попробуйте ещё раз через минуту.'), 503
        if r.status_code in (200, 201) and r.json().get('pay_url'):
            return redirect(r.json()['pay_url'], code=303)
        if r.status_code == 202:
            return _message('Заявка принята', 'Ссылку на оплату пришлём на почту в ближайшее время.'), 202
        if r.status_code == 422:
            fields = '; '.join(r.json().get('fields', {}).values())
            return _message('Проверьте данные формы', fields), 422
        return _message('Не удалось оформить заказ', 'Напишите нам, мы разберёмся.'), 502

    return app


SHOP_CSS = """
.shop{max-width:560px;margin:10px auto}.offers{display:grid;gap:8px}
.offer{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:9px;padding:10px 12px;
margin:0;cursor:pointer;color:var(--ink);font-size:15px}.offer input{width:auto;margin-top:4px}
"""


def _message(title: str, text: str) -> str:
    return page(title, f'<div class="shop card"><h1>{escape(title)}</h1><p>{escape(text)}</p>'
                       '<p><a href="/">← К форме</a></p></div>', RIBBON, SHOP_CSS)
