#!/usr/bin/env python3
"""Рисует схему docs/sequence.svg (последовательность обмена на стенде). Запуск: python docs/make_diagram.py"""
from __future__ import annotations

import pathlib
from html import escape

OUT = pathlib.Path(__file__).with_name('sequence.svg')
W = 1580
INK, MUTED, LINE, ACCENT = '#1f2a33', '#5d6a76', '#c9d1d8', '#2b6f8e'
FONT = '"Segoe UI","DejaVu Sans",Arial,sans-serif'

LANES = [('client', 'Клиент и сайт', 'форма заказа'),
         ('pay', 'Платёжный сервис', 'учебный, не настоящий'),
         ('svc', 'Сервис интеграции', 'его пишет исполнитель'),
         ('crm', 'CRM', 'учебная, не настоящая'),
         ('msg', 'Почта и Telegram', 'учебные, письма не уходят')]
X = {key: 255 + i * 292 for i, (key, _, _) in enumerate(LANES)}
HEAD_Y, HEAD_H = 118, 54           # верх и высота заголовков лайфлайнов
ROW = 62

# kind: msg (from, to, строка 1, строка 2, пунктир) | note (lane, строка 1, строка 2, ширина) | phase (название)
ROWS = [
    ('phase', '1  Заявка с сайта'),
    ('msg', 'client', 'svc', 'POST /leads', 'идентификатор заявки, имя, почта, продукт', False),
    ('note', 'svc', 'Заказ записан в базу', 'цена из каталога на сервере; повтор заявки вернёт тот же заказ', 450),
    ('msg', 'svc', 'crm', 'Создать сделку', 'ключ: номер заказа, дубль не создаётся', False),
    ('msg', 'svc', 'pay', 'Создать счёт', 'Idempotence-Key = номер заказа', False),
    ('msg', 'pay', 'svc', 'Ссылка на оплату', '', True),
    ('msg', 'svc', 'crm', 'Стадия «Ждёт оплаты»', 'и комментарий в сделке', False),
    ('msg', 'svc', 'msg', 'Письмо со ссылкой на оплату', '', False),
    ('msg', 'svc', 'client', 'Ответ сайту: ссылка на оплату', 'повторная заявка получит тот же ответ', True),
    ('phase', '2  Оплата'),
    ('msg', 'client', 'pay', 'Оплата на странице провайдера', 'учебная страница, деньги не списываются', False),
    ('msg', 'pay', 'svc', 'Вебхук «платёж прошёл»', 'подпись HMAC-SHA256 и время отправки', False),
    ('note', 'svc', 'Проверки до любых действий', 'подпись верна · событие новое · сумма и валюта как в заказе', 440),
    ('group', 'Очередь шагов: каждый шаг запоминается и при сбое повторяется'),
    ('msg', 'svc', 'msg', 'Письмо с чеком и доступом', '', False),
    ('msg', 'svc', 'crm', 'Стадии «Оплачено» и «Доступ выдан»', 'с комментариями и номером чека', False),
    ('msg', 'svc', 'msg', 'Сообщение менеджеру в Telegram', '', False),
    ('endgroup',),
    ('msg', 'svc', 'pay', 'Ответ 200: событие принято', '', True),
]

SIDE = [('Вебхук пришёл дважды', 'второй раз ничего не меняется: событие запоминается по идентификатору'),
        ('Вебхук подделан', 'нет верной подписи или она старая: ответ 401, заказ не тронут'),
        ('Сумма не сходится с заказом', 'статус не меняется, менеджеру уходит предупреждение'),
        ('CRM не отвечает', 'клиент всё равно получает доступ, шаги CRM остаются в очереди, их дожимает сверка'),
        ('Сервис перезапускается', 'платёжный сервис повторяет доставку вебхука, заказ обработан один раз'),
        ('Вебхук потерялся совсем', 'сверка спрашивает у платёжного сервиса статус счетов и запускает обычную обработку')]


def t(x, y, s, size=13, weight=400, fill=INK, anchor='middle', halo=True):
    h = ' stroke="#fff" stroke-width="5" stroke-linejoin="round" paint-order="stroke"' if halo else ''
    return (f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}"{h}>{escape(s)}</text>')


def build() -> str:
    body_y0 = HEAD_Y + HEAD_H + 14
    # сначала считаем высоту: каждая строка занимает свою долю
    heights = {'phase': ROW - 8, 'note': ROW + 14, 'msg': ROW, 'group': 22, 'endgroup': 14}
    body_h = sum(heights[r[0]] for r in ROWS) + 30
    side_y = body_y0 + body_h + 26
    H = side_y + 34 + 2 * 94 + 28
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family=\'{FONT}\'>',
         '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
         f'<path d="M0 0L10 5L0 10z" fill="{INK}"/></marker></defs>',
         f'<rect width="{W}" height="{H}" fill="#fff"/>',
         f'<rect width="{W}" height="34" fill="#fff3d6"/>',
         t(W / 2, 22, 'ТЕСТОВЫЙ СТЕНД: схема работы тестового стенда. CRM, платёжный сервис, почта и сайт вымышленные, деньги и письма настоящими не бывают',
           13, 600, '#7a4b00', halo=False),
         t(48, 68, 'Заявка, оплата и доступ: как обмениваются системы', 24, 700, INK, 'start', False),
         t(48, 92, 'Сервис интеграции связывает сайт, CRM и платёжный сервис. Сплошная стрелка: запрос, пунктирная: ответ. '
                   'Схема построена по коду проекта и его тестовому прогону.', 13, 400, MUTED, 'start', False)]
    for key, title, sub in LANES:                                  # лайфлайны и заголовки
        x = X[key]
        accent = key == 'svc'
        o.append(f'<line x1="{x}" y1="{HEAD_Y + HEAD_H}" x2="{x}" y2="{body_y0 + body_h - 14}" stroke="{ACCENT if accent else LINE}" '
                 f'stroke-width="{2.5 if accent else 1.5}" stroke-dasharray="{"" if accent else "5 5"}"/>')
        o.append(f'<rect x="{x - 128}" y="{HEAD_Y}" width="256" height="{HEAD_H}" rx="9" fill="{ACCENT if accent else "#f1f4f6"}" '
                 f'stroke="{ACCENT if accent else LINE}"/>')
        o.append(t(x, HEAD_Y + 24, title, 15, 700, '#fff' if accent else INK, halo=False))
        o.append(t(x, HEAD_Y + 43, sub, 11.5, 400, '#dbe9f0' if accent else MUTED, halo=False))
    y = body_y0 + 6
    group_at = None
    for row in ROWS:
        kind = row[0]
        if kind == 'phase':
            o.append(f'<rect x="24" y="{y - 4}" width="{W - 48}" height="30" rx="6" fill="#eef3f6"/>')
            o.append(t(40, y + 16, row[1], 14.5, 700, ACCENT, 'start', False))
            y += heights['phase'] + 14
        elif kind == 'group':
            group_at = (y, len(o), row[1])
            y += heights['group'] + 10
        elif kind == 'endgroup':
            gy, pos, label = group_at
            gx0, gx1 = X['svc'] - 34, X['msg'] + 70
            rect = (f'<rect x="{gx0}" y="{gy - 6}" width="{gx1 - gx0}" height="{y - gy - 4}" rx="9" fill="#fbfcfd" '
                    f'stroke="{ACCENT}" stroke-dasharray="6 4" stroke-width="1.2"/>'
                    + t(gx0 + 12, gy + 12, label, 12.5, 700, ACCENT, 'start', False))
            o.insert(pos, rect)                                    # рамка под стрелками
            y += heights['endgroup']
        elif kind == 'note':
            _, lane, l1, l2, wid = row
            x = X[lane] - 14
            o.append(f'<rect x="{x}" y="{y - 4}" width="{wid}" height="50" rx="7" fill="#e8f1f6" stroke="{ACCENT}" stroke-width="1.2"/>')
            o.append(t(x + 12, y + 15, l1, 13.5, 700, INK, 'start', False))
            o.append(t(x + 12, y + 33, l2, 12, 400, MUTED, 'start', False))
            y += heights['note']
        else:
            _, a, b, l1, l2, dashed = row
            xa, xb = X[a], X[b]
            d = 1 if xb > xa else -1
            ya = y + 30
            dash = 'stroke-dasharray="6 4" ' if dashed else ''
            o.append(f'<line x1="{xa + 5 * d}" y1="{ya}" x2="{xb - 2 * d}" y2="{ya}" stroke="{INK}" stroke-width="1.6" {dash}marker-end="url(#a)"/>')
            cx = (xa + xb) / 2
            if l2:
                o.append(t(cx, ya - 21, l1, 13.5, 600))
                o.append(t(cx, ya - 6, l2, 12, 400, MUTED))
            else:
                o.append(t(cx, ya - 8, l1, 13.5, 600))
            y += heights['msg']
    o.append(t(48, side_y + 14, 'Что происходит при сбоях (каждый случай есть в тестовом прогоне и в тестах)', 17, 700, INK, 'start', False))
    colw = (W - 96 - 2 * 20) / 3
    for i, (title, text) in enumerate(SIDE):
        cx0 = 48 + (i % 3) * (colw + 20)
        cy0 = side_y + 34 + (i // 3) * 94
        o.append(f'<rect x="{cx0:.0f}" y="{cy0}" width="{colw:.0f}" height="82" rx="9" fill="#f6f8f9" stroke="{LINE}"/>')
        o.append(t(cx0 + 14, cy0 + 24, title, 14, 700, INK, 'start', False))
        lines, cur = [], ''
        for w in text.split():
            if len(cur) + len(w) + 1 > 64:
                lines.append(cur)
                cur = w
            else:
                cur = f'{cur} {w}'.strip()
        lines.append(cur)
        for j, ln in enumerate(lines[:3]):
            o.append(t(cx0 + 14, cy0 + 44 + j * 16, ln, 12.5, 400, MUTED, 'start', False))
    o.append('</svg>')
    return '\n'.join(o)


if __name__ == '__main__':
    OUT.write_text(build(), encoding='utf-8')
    print('записано', OUT, OUT.stat().st_size, 'байт')
