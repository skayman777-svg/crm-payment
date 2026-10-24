"""Форматирование сумм для писем и комментариев."""
from __future__ import annotations

NBSP = ' '


def rub(kop) -> str:
    """590000 -> «5 900 ₽», 12345 -> «123,45 ₽» (неразрывные пробелы)."""
    if kop is None:
        return '—'
    try:
        r, k = divmod(int(kop), 100)
    except (TypeError, ValueError):
        return str(kop)[:30]                 # в уведомлении пришла не сумма: показываем как есть, но не падаем
    s = f'{r:,}'.replace(',', NBSP)
    return f'{s},{k:02d}{NBSP}₽' if k else f'{s}{NBSP}₽'
