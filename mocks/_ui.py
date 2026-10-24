"""Общая обёртка для учебных страниц (CRM, оплата, сайт): один файл стилей, без внешних шрифтов и скриптов."""
from __future__ import annotations

import re
from html import escape

CSS = """
:root{--ink:#1f2a33;--muted:#6b7783;--line:#dfe4e8;--bg:#f3f5f7;--card:#fff;--accent:#2b6f8e;
--warn-bg:#fff3d6;--warn-ink:#7a4b00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI","DejaVu Sans",system-ui,sans-serif}
.ribbon{background:var(--warn-bg);color:var(--warn-ink);padding:8px 20px;font-size:13px;font-weight:600}
.wrap{padding:22px 20px 40px;max-width:1240px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.muted{color:var(--muted)}.num{font-variant-numeric:tabular-nums}
.btn{display:inline-block;border:0;border-radius:8px;padding:11px 18px;font:600 15px inherit;font-family:inherit;
cursor:pointer;background:var(--accent);color:#fff;text-decoration:none}
.btn.alt{background:#fff;color:var(--ink);border:1px solid var(--line)}
a{color:var(--accent)}input[type=radio]{accent-color:var(--accent)}
input,select{font:inherit;padding:9px 10px;border:1px solid var(--line);border-radius:8px;width:100%;background:#fff;color:var(--ink)}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
"""


def page(title: str, body: str, ribbon: str, extra_css: str = '') -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><style>{CSS}{extra_css}</style></head><body>'
            f'<div class="ribbon">{escape(ribbon)}</div><div class="wrap">{body}</div></body></html>')


def short(text: str, n: int) -> str:
    """Обрезка для строки журнала по границе слова: «текст текст…»."""
    text = re.sub(r'[ \t\r\n]+', ' ', text)               # неразрывные пробелы (между числом и ₽) не трогаем
    if len(text) <= n:
        return text
    return text[:n].rsplit(' ', 1)[0].rstrip(',.;:') + '…'


def rub(kop) -> str:
    """59000 коп. -> «590 ₽»; 590000 -> «5 900 ₽» (неразрывные пробелы)."""
    if kop is None:
        return '—'
    r, k = divmod(int(kop), 100)
    s = f'{r:,}'.replace(',', ' ')
    return f'{s},{k:02d} ₽' if k else f'{s} ₽'
