"""Настройки и каталог продуктов. Секреты берутся из переменных окружения, в коде их нет."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Product:
    code: str
    title: str
    price_kop: int                  # цена в копейках; цену всегда определяет сервер, а не форма на сайте


CATALOG: Dict[str, Product] = {p.code: p for p in (
    Product('consult-60', 'Консультация 60 минут', 350_000),
    Product('excel-mini', 'Мини-курс «Excel для учёта»', 590_000),
    Product('audit', 'Аудит рабочих процессов', 1_200_000),
)}


@dataclass
class Settings:
    crm_url: str
    crm_token: str
    pay_url: str
    pay_key: str
    webhook_secret: str
    msg_url: str
    lead_token: str = 'site-demo-token'        # ключ, с которым сайт обращается к сервису
    access_secret: str = 'access-demo-secret'  # из него считаются персональные ссылки доступа
    db_path: str = ':memory:'
    manager_chat: str = '@demo_manager'
    access_base: str = 'https://school.example/access'
    tolerance_s: int = 300                     # допустимое расхождение времени в подписи вебхука
    http_attempts: int = 3                     # попыток на один запрос к внешней системе
    http_backoff: float = 0.2                  # пауза перед повтором, удваивается с каждой попыткой
    http_jitter: bool = True
    http_timeout: float = 3.0
    step_max_attempts: int = 8                 # после стольких неудачных проходов шаг считается «мёртвым»
    poll_after_s: int = 600                    # заказ без оплаты дольше этого времени сверяем с провайдером
    catalog: Dict[str, Product] = field(default_factory=lambda: dict(CATALOG))

    @classmethod
    def from_env(cls) -> 'Settings':
        def need(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise SystemExit(f'Не задана переменная окружения {name}')
            return value
        return cls(crm_url=need('CRM_URL'), crm_token=need('CRM_TOKEN'), pay_url=need('PAY_URL'),
                   pay_key=need('PAY_KEY'), webhook_secret=need('WEBHOOK_SECRET'), msg_url=need('MSG_URL'),
                   lead_token=need('LEAD_TOKEN'), access_secret=need('ACCESS_SECRET'),
                   db_path=os.environ.get('DB_PATH', 'integration.db'),
                   manager_chat=os.environ.get('MANAGER_CHAT', cls.manager_chat),
                   access_base=os.environ.get('ACCESS_BASE', cls.access_base))
