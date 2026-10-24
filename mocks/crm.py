"""Учебная CRM: сделки, стадии, комментарии. Не настоящая CRM и не повторяет API конкретного продукта.

Нужна, чтобы показать интеграцию: идемпотентное создание сделки, проверка допустимых переходов между стадиями,
авторизация по токену и имитация сбоев (503) для проверки повторных попыток. Комментарии принимают `uid`:
повторная отправка с тем же `uid` не создаёт дубль.
"""
from __future__ import annotations

import itertools
import logging
import threading
from datetime import datetime
from html import escape

from flask import Flask, jsonify, request

from ._ui import page, rub, short

log = logging.getLogger('crm')

STAGES = [                                     # код, название, цвет колонки
    ('NEW', 'Новая заявка', '#6b7783'),
    ('AWAITING_PAYMENT', 'Ждёт оплаты', '#c27a00'),
    ('PAYMENT_FAILED', 'Оплата не прошла', '#b83a2b'),
    ('PAID', 'Оплачено', '#2b6f8e'),
    ('ACCESS_GRANTED', 'Доступ выдан', '#2e8b57'),
]
STAGE_RU = {code: name for code, name, _ in STAGES}
ALLOWED = {
    'NEW': {'AWAITING_PAYMENT'},
    'AWAITING_PAYMENT': {'PAID', 'PAYMENT_FAILED'},
    'PAYMENT_FAILED': {'AWAITING_PAYMENT', 'PAID'},
    'PAID': {'ACCESS_GRANTED'},
    'ACCESS_GRANTED': set(),
}
FIELD_RU = [('invoice_id', 'Счёт'), ('pay_url', 'Ссылка на оплату'), ('receipt', 'Чек'), ('access_url', 'Доступ')]


class CRMState:
    def __init__(self):
        self.deals = {}
        self.by_ext = {}
        self.seq = itertools.count(101)
        self.fail = 0                       # сколько ближайших запросов к /api ответят 503
        self.requests = 0
        self.lock = threading.Lock()


def create_app(token: str = 'crm-demo-token'):
    app = Flask('crm-mock')
    app.json.ensure_ascii = False
    st = CRMState()

    @app.before_request
    def guard():
        if not request.path.startswith('/api/'):
            return None
        with st.lock:
            st.requests += 1
            if st.fail > 0:
                st.fail -= 1
                log.warning('%s %s → 503 (имитация сбоя)', request.method, request.path)
                return jsonify(error='temporarily unavailable'), 503
        if request.headers.get('Authorization') != f'Bearer {token}':
            log.warning('%s %s → 401 (неверный токен)', request.method, request.path)
            return jsonify(error='unauthorized'), 401
        return None

    @app.post('/api/deals')
    def create_deal():
        body = request.get_json(silent=True) or {}
        ext = body.get('external_id')
        if not ext:
            return jsonify(error='external_id is required'), 422
        with st.lock:
            if ext in st.by_ext:                                      # повторный запрос: отдаём ту же сделку
                deal = st.deals[st.by_ext[ext]]
                log.info('POST /api/deals external_id=%s → 200: сделка #%s уже есть, дубль не создан', ext, deal['id'])
                return jsonify(deal), 200
            did = str(next(st.seq))
            deal = {'id': did, 'external_id': ext, 'title': body.get('title', ''), 'amount_kop': body.get('amount_kop'),
                    'contact': body.get('contact', {}), 'stage': 'NEW', 'fields': {}, 'timeline': []}
            st.deals[did], st.by_ext[ext] = deal, did
        log.info('POST /api/deals external_id=%s → 201: создана сделка #%s', ext, did)
        return jsonify(deal), 201

    @app.get('/api/deals/<did>')
    def get_deal(did):
        deal = st.deals.get(did)
        return (jsonify(deal), 200) if deal else (jsonify(error='not found'), 404)

    @app.patch('/api/deals/<did>')
    def patch_deal(did):
        body = request.get_json(silent=True) or {}
        with st.lock:
            deal = st.deals.get(did)
            if not deal:
                return jsonify(error='not found'), 404
            new = body.get('stage')
            if new and new != deal['stage']:
                if new not in ALLOWED.get(deal['stage'], set()):
                    log.warning('PATCH /api/deals/%s → 409: переход %s → %s запрещён', did, deal['stage'], new)
                    return jsonify(error=f'transition {deal["stage"]} -> {new} is not allowed'), 409
                log.info('PATCH /api/deals/%s → 200: стадия %s → %s', did, deal['stage'], new)
                deal['stage'] = new
            deal['fields'].update(body.get('fields') or {})
        return jsonify(deal), 200

    @app.post('/api/deals/<did>/comments')
    def add_comment(did):
        body = request.get_json(silent=True) or {}
        uid = body.get('uid')
        with st.lock:
            deal = st.deals.get(did)
            if not deal:
                return jsonify(error='not found'), 404
            if uid and any(c.get('uid') == uid for c in deal['timeline']):
                log.info('POST /api/deals/%s/comments uid=%s → 200: комментарий уже есть, дубль не создан', did, uid)
                return jsonify(ok=True, duplicate=True), 200
            deal['timeline'].append({'at': datetime.now().strftime('%H:%M:%S'), 'uid': uid, 'text': body.get('text', '')})
        log.info('POST /api/deals/%s/comments → 201: «%s»', did, short(body.get('text', ''), 90))
        return jsonify(ok=True), 201

    # --- управление имитацией (не часть «боевого» API)
    @app.post('/_sim/fail')
    def sim_fail():
        with st.lock:
            st.fail = int((request.get_json(silent=True) or {}).get('count', 0))
        return jsonify(ok=True)

    # --- простая веб-страница: доска сделок и карточка (для скриншотов и ручной проверки)
    @app.get('/')
    def board():
        cols = []
        for code, name, color in STAGES:
            deals = [d for d in st.deals.values() if d['stage'] == code]
            cards = ''.join(_card(d) for d in deals) or '<div class="empty">пусто</div>'
            cols.append(f'<section class="col"><h2 style="border-color:{color}">{escape(name)}'
                        f'<span class="cnt">{len(deals)}</span></h2>{cards}</section>')
        body = ('<h1>Учебная CRM · доска сделок</h1><p class="sub">Вымышленная CRM для проверки интеграции. '
                'Клиенты и суммы придуманы.</p><div class="board">' + ''.join(cols) + '</div>')
        return page('Учебная CRM', body, 'ТЕСТОВЫЙ СТЕНД: учебная CRM, не настоящая система', BOARD_CSS)

    @app.get('/deals/<did>')
    def card(did):
        d = st.deals.get(did)
        if not d:
            return 'not found', 404
        return page(f'Сделка #{did}', _deal_page(d), 'ТЕСТОВЫЙ СТЕНД: учебная CRM, не настоящая система', BOARD_CSS)

    app.crm_state = st
    return app


BOARD_CSS = """
.board{display:grid;grid-template-columns:repeat(5,minmax(210px,1fr));gap:12px;align-items:start;overflow-x:auto}
.col h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;margin:0 0 10px;padding-bottom:6px;
border-bottom:3px solid;display:flex;justify-content:space-between}
.cnt{color:var(--muted);font-weight:600}
.deal{display:block;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:10px 12px;
margin-bottom:10px;color:inherit;text-decoration:none}
.deal b{display:block;font-size:14px;line-height:1.3}.deal .m{font-size:12.5px;color:var(--muted)}
.deal .amt{font-weight:700;margin-top:4px}.deal .last{margin-top:6px;font-size:12.5px;color:#45525e;
border-top:1px dashed var(--line);padding-top:6px}
.empty{color:var(--muted);font-size:13px;padding:6px 2px}
.flow{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 18px}
.step{padding:6px 14px;border-radius:999px;background:#e9edf0;color:var(--muted);font-size:13px;font-weight:600}
.step.on{color:#fff}.step.past{background:#d7ebe0;color:#215c3a}
.grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:16px}
@media(max-width:760px){.grid{grid-template-columns:1fr}.board{grid-template-columns:repeat(5,230px)}}
dl{margin:0;display:grid;grid-template-columns:130px 1fr;gap:8px 10px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.tl{list-style:none;margin:0;padding:0}.tl li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:12px}
.tl li:last-child{border:0}.tl time{color:var(--muted);flex:none;font-variant-numeric:tabular-nums}
"""


def _card(d: dict) -> str:
    last = d['timeline'][-1]['text'] if d['timeline'] else ''
    contact = d.get('contact') or {}
    return (f'<a class="deal" href="/deals/{escape(d["id"])}"><b>{escape(d["title"])}</b>'
            f'<div class="m">{escape(contact.get("name", ""))} · {escape(contact.get("email", ""))}</div>'
            f'<div class="amt num">{rub(d["amount_kop"])}</div>'
            + (f'<div class="last">{escape(short(last, 120))}</div>' if last else '') + '</a>')


def _deal_page(d: dict) -> str:
    main = [s for s in STAGES if s[0] != 'PAYMENT_FAILED']
    order = [s[0] for s in main]
    cur = d['stage']
    chips = []
    for code, name, color in main:
        if cur == code:
            chips.append(f'<span class="step on" style="background:{color}">{escape(name)}</span>')
        elif cur in order and order.index(code) < order.index(cur):
            chips.append(f'<span class="step past">✓ {escape(name)}</span>')
        else:
            chips.append(f'<span class="step">{escape(name)}</span>')
    if cur == 'PAYMENT_FAILED':
        chips.insert(2, '<span class="step on" style="background:#b83a2b">Оплата не прошла</span>')
    contact = d.get('contact') or {}
    rows = [('Клиент', contact.get('name', '')), ('Email', contact.get('email', '')), ('Сумма', rub(d['amount_kop']))]
    rows += [(label, d['fields'][key]) for key, label in FIELD_RU if d['fields'].get(key)]
    dl = ''.join(f'<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>' for k, v in rows)
    tl = ''.join(f'<li><time>{escape(c["at"])}</time><span>{escape(c["text"])}</span></li>' for c in d['timeline'])
    return (f'<p class="sub"><a href="/">← Доска сделок</a></p><h1>{escape(d["title"])}</h1>'
            f'<p class="sub">Сделка #{escape(d["id"])}</p><div class="flow">{"".join(chips)}</div>'
            f'<div class="grid"><div class="card"><dl>{dl}</dl></div>'
            f'<div class="card"><b>История</b><ul class="tl">{tl or "<li>пока пусто</li>"}</ul></div></div>')
