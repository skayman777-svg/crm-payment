"""Логика интеграции без привязки к веб-фреймворку.

Путь заказа:  заявка → заказ в БД → [сделка в CRM, счёт в платёжном сервисе, стадия «Ждёт оплаты», письмо со ссылкой]
              → вебхук об оплате → [доступ, письмо с чеком, стадии «Оплачено» и «Доступ выдан», сообщение менеджеру].
Каждое действие во внешней системе оформлено как шаг в очереди (таблица steps): шаг идемпотентен, при сбое
остаётся в очереди, а `reconcile()` (по расписанию) его дожимает. Так сбой CRM или почты не теряет оплаченный заказ.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
from typing import Dict, Iterable, Optional, Tuple

from .clients import CRM, Notifier, Pay
from .config import Settings
from .fmt import rub
from .http import Api, ApiError, Unavailable
from .signature import SignatureError, verify
from .store import Store

log = logging.getLogger('svc')

EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PAID_STATES = ('PAID', 'ACCESS_GRANTED')
LEAD_PLAN = [('crm_deal', 'CRM'), ('invoice', 'PAY'), ('crm_awaiting', 'CRM'), ('notify_link', 'MSG')]
PAID_PLAN = [('grant_access', 'LOCAL'), ('notify_paid', 'MSG'), ('crm_paid', 'CRM'), ('crm_done', 'CRM'),
             ('notify_manager', 'MSG')]
ORDERED = {'CRM'}              # шаги этих систем идут строго по порядку: стадию сделки нельзя перепрыгнуть
REASON_RU = {'insufficient_funds': 'недостаточно средств', 'card_declined': 'банк отклонил операцию'}


class Wait(Exception):
    """Шаг ждёт результата другого шага (например, номера сделки). Это не ошибка."""


class Service:
    def __init__(self, cfg: Settings, store: Optional[Store] = None, crm: Optional[CRM] = None,
                 pay: Optional[Pay] = None, notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)

        def api(name: str, url: str, headers: dict) -> Api:
            return Api(name, url, headers, attempts=cfg.http_attempts, backoff=cfg.http_backoff,
                       jitter=cfg.http_jitter, timeout=cfg.http_timeout)
        self.crm = crm or CRM(api('CRM', cfg.crm_url, {'Authorization': f'Bearer {cfg.crm_token}'}))
        self.pay = pay or Pay(api('Платёжный сервис', cfg.pay_url, {'Authorization': f'Bearer {cfg.pay_key}'}))
        self.notifier = notifier or Notifier(api('Почта и Telegram', cfg.msg_url, {}))
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.handlers = {
            'crm_deal': self._s_crm_deal, 'invoice': self._s_invoice, 'crm_awaiting': self._s_crm_awaiting,
            'notify_link': self._s_notify_link, 'grant_access': self._s_grant_access,
            'notify_paid': self._s_notify_paid, 'crm_paid': self._s_crm_paid, 'crm_done': self._s_crm_done,
            'notify_manager': self._s_notify_manager, 'crm_failed': self._s_crm_failed,
            'notify_failed': self._s_notify_failed, 'crm_alert': self._s_crm_alert, 'notify_alert': self._s_notify_alert,
        }

    # ================================================================== заявка с сайта
    def accept_lead(self, data: dict) -> Tuple[int, dict]:
        errors = self._validate(data)
        if errors:
            log.warning('заявка отклонена (422): %s', '; '.join(f'{k}: {v}' for k, v in errors.items()))
            return 422, {'error': 'validation', 'fields': errors}
        lead_id, name, email = data['lead_id'].strip(), data['name'].strip(), data['email'].strip()
        product = self.cfg.catalog[data['product']]
        if 'amount' in data or 'amount_kop' in data:
            log.warning('заявка %s: сумма из формы проигнорирована, цену задаёт каталог на сервере', lead_id)
        fp = hashlib.sha256(json.dumps([name, email, product.code], ensure_ascii=False).encode()).hexdigest()
        order, created = self.store.create_order(lead_id, fp, name, email, product.code, product.title,
                                                 product.price_kop, LEAD_PLAN)
        ref = order['ref']
        if not created and order['fingerprint'] != fp:
            log.warning('заявка %s (409): идентификатор уже использован с другими данными, заказ %s не тронут', lead_id, ref)
            return 409, {'error': 'lead_id was already used with different data'}
        if created:
            log.info('заявка %s принята: заказ %s, %s, %s (цена из каталога)', lead_id, ref, product.title, rub(product.price_kop))
        else:
            log.info('заявка %s уже принята (заказ %s), дубль не создан', lead_id, ref)
        self.run_pending(ref)
        order = self.store.order(ref)
        code = (201 if created else 200) if order['pay_url'] else 202
        return code, {'order_id': ref, 'status': order['status'], 'pay_url': order['pay_url'], 'duplicate': not created}

    def _validate(self, data: dict) -> Dict[str, str]:
        bad: Dict[str, str] = {}

        def text(key: str) -> str:
            v = data.get(key)
            return v.strip() if isinstance(v, str) else ''
        if not 1 <= len(text('lead_id')) <= 64:
            bad['lead_id'] = 'нужен идентификатор заявки до 64 символов'
        if not 1 <= len(text('name')) <= 100:
            bad['name'] = 'укажите имя (до 100 символов)'
        email = text('email')
        if not EMAIL.match(email) or len(email) > 254:
            bad['email'] = 'неверный адрес почты'
        product = data.get('product')
        if not isinstance(product, str) or product not in self.cfg.catalog:
            bad['product'] = 'неизвестный продукт'
        return bad

    # ================================================================== вебхук провайдера
    def handle_webhook(self, raw: bytes, header: Optional[str]) -> Tuple[int, dict]:
        try:
            verify(self.cfg.webhook_secret, raw, header, self.cfg.tolerance_s)
        except SignatureError as exc:
            log.warning('вебхук отклонён (401): %s', exc)
            return 401, {'error': 'invalid signature'}                  # причину провайдеру не сообщаем
        try:
            evt = json.loads(raw)
            evt_id, etype, data = str(evt['id']), str(evt['type']), evt['data']
            ref = str(data['order_ref'])
        except (ValueError, KeyError, TypeError):
            log.warning('вебхук отклонён (400): тело запроса не разобрано')
            return 400, {'error': 'bad payload'}
        return self._process_event(evt_id, etype, ref, data, verified=True)

    def _process_event(self, evt_id: str, etype: str, ref: str, data: dict, verified: bool,
                       acc: Optional[Dict[str, int]] = None) -> Tuple[int, dict]:
        head = f'{"вебхук" if verified else "сверка, событие"} {evt_id} {etype}'
        order = self.store.order(ref)
        if order is None:
            self.store.record_event(evt_id, etype, ref)
            self.store.set_event_outcome(evt_id, 'ignored:unknown_order')
            log.warning('%s: заказа %s у нас нет, событие пропущено', head, ref)
            return 200, {'status': 'ignored', 'reason': 'unknown_order'}
        if order['status'] == 'NEW':
            log.warning('%s: счёт по заказу %s ещё не записан у нас, просим провайдера повторить позже (503)', head, ref)
            return 503, {'error': 'order is not ready, retry later'}
        with self.store.tx():                       # дедупликация, смена статуса и план шагов: одним действием
            first = self.store.record_event(evt_id, etype, ref)
            if first:
                outcome, text = self._decide(self.store.order(ref), evt_id, etype, data)
                self.store.set_event_outcome(evt_id, outcome)
        if not first:
            log.info('%s: уже обработан, повтор ничего не меняет', head)
            self._run(ref, acc)                     # заодно дожимаем отложенные шаги этого заказа
            return 200, {'status': 'duplicate'}
        log.info('%s: %s%s', head, 'подпись верна, ' if verified else '', text)
        self._run(ref, acc)
        return 200, {'status': outcome}

    def _decide(self, o: dict, eid: str, etype: str, data: dict) -> Tuple[str, str]:
        ref = o['ref']
        if data.get('invoice_id') != o['invoice_id']:
            return 'ignored:other_invoice', f'счёт {data.get("invoice_id")} не относится к заказу {ref}, событие пропущено'
        if etype == 'payment.succeeded':
            if o['status'] in PAID_STATES:
                if data.get('payment_id') == o['payment_id']:
                    return 'ignored:already_paid', f'заказ {ref} уже оплачен этим платежом, событие пропущено'
                self._alert(ref, eid, f'⚠ По заказу {ref} пришёл второй платёж ({data.get("payment_id")}) '
                                      'при уже оплаченном счёте. Возможна двойная оплата, нужна ручная проверка.')
                return 'needs_review:second_payment', f'заказ {ref}: второй платёж по оплаченному счёту, сообщаем менеджеру'
            if data.get('amount_kop') != o['amount_kop'] or data.get('currency') != o['currency']:
                got = rub(data.get('amount_kop')) + ('' if data.get('currency') == o['currency'] else f' ({data.get("currency")})')
                self._alert(ref, eid, f'⚠ Заказ {ref}: в уведомлении об оплате сумма {got}, а в заказе {rub(o["amount_kop"])}. '
                                      'Статус не менялся, нужна ручная проверка.')
                return ('rejected:amount_mismatch', f'сумма {rub(data.get("amount_kop"))} не совпала с заказом '
                                                    f'({rub(o["amount_kop"])}), статус {ref} не изменён, менеджеру отправлено предупреждение')
            self.store.set_status(ref, 'PAID', ('AWAITING_PAYMENT', 'PAYMENT_FAILED'))
            self.store.update_order(ref, payment_id=data.get('payment_id'), receipt=(data.get('receipt') or {}).get('number'))
            self._plan(ref, PAID_PLAN)
            return 'paid', f'заказ {ref} оплачен ({rub(o["amount_kop"])}) → PAID'
        if etype == 'payment.failed':
            if o['status'] in PAID_STATES:
                return 'ignored:late_failed_after_paid', f'заказ {ref} уже оплачен, запоздавшее «платёж не прошёл» пропущено'
            self.store.set_status(ref, 'PAYMENT_FAILED', ('AWAITING_PAYMENT', 'PAYMENT_FAILED'))
            self._plan(ref, [(f'crm_failed:{eid}', 'CRM'), (f'notify_failed:{eid}', 'MSG')], {'reason': data.get('reason', '')})
            return 'failed', f'платёж отклонён ({REASON_RU.get(data.get("reason"), data.get("reason"))}), заказ {ref} → PAYMENT_FAILED'
        return 'ignored:unknown_type', f'тип события «{etype}» не обрабатывается'

    def _plan(self, ref: str, plan: Iterable[Tuple[str, str]], payload: Optional[dict] = None) -> None:
        for key, system in plan:
            self.store.add_step(ref, key, system, payload)

    def _alert(self, ref: str, eid: str, text: str) -> None:
        self._plan(ref, [(f'crm_alert:{eid}', 'CRM'), (f'notify_alert:{eid}', 'MSG')], {'text': text})

    # ================================================================== очередь шагов
    def _lock_for(self, ref: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(ref, threading.Lock())

    def _run(self, ref: str, acc: Optional[Dict[str, int]]) -> None:
        """Выполняет шаги заказа; итоги складывает в acc (так сверка считает и то, что сделано при поиске оплат)."""
        res = self.run_pending(ref)
        if acc is not None:
            for k, v in res.items():
                acc[k] += v

    def run_pending(self, ref: Optional[str] = None) -> Dict[str, int]:
        total = {'done': 0, 'pending': 0, 'dead': 0}
        for r in ([ref] if ref else self.store.refs_with_pending()):
            with self._lock_for(r):                 # два параллельных запуска не выполнят один шаг дважды
                part = self._run_order(r)
            for k in total:
                total[k] += part[k]
        return total

    def _run_order(self, ref: str) -> Dict[str, int]:
        res = {'done': 0, 'pending': 0, 'dead': 0}
        blocked, failed = set(), set()             # недоступные системы и упавшие шаги этого запуска
        halted = set(self.store.dead_systems(ref)) & ORDERED   # мёртвый шаг цепочки держит остальные до ручного разбора
        for _ in range(3):                         # шаг может разблокировать следующий: делаем до трёх проходов
            progress = False
            for st in self.store.pending_steps(ref):
                if st['system'] in blocked or st['system'] in halted or st['id'] in failed:
                    continue
                try:
                    self.handlers[st['key'].split(':')[0]](self.store.order(ref), st['key'], st['payload'])
                except Wait:
                    continue
                except Exception as exc:                # сбой одного шага не должен останавливать остальные
                    failed.add(st['id'])
                    if isinstance(exc, Unavailable):
                        blocked.add(st['system'])   # система лежит: остальные её шаги в этом запуске не мучаем
                    elif not isinstance(exc, ApiError):
                        log.exception('заказ %s: шаг %s упал с неожиданной ошибкой', ref, st['key'])
                    if st['system'] in ORDERED:
                        halted.add(st['system'])    # следующие шаги цепочки не обгоняют упавший
                    if self.store.fail_step(st['id'], f'{type(exc).__name__}: {exc}', self.cfg.step_max_attempts) == 'dead':
                        res['dead'] += 1
                        log.error('заказ %s: шаг %s не удался %d раз, нужна ручная проверка: %s', ref, st['key'],
                                  self.cfg.step_max_attempts, exc)
                    else:
                        log.warning('заказ %s: шаг %s отложен: %s', ref, st['key'], exc)
                    continue
                self.store.finish_step(st['id'])
                res['done'] += 1
                progress = True
                if st['attempts']:
                    log.info('заказ %s: шаг %s выполнен после сбоев (неудачных проходов было: %d)', ref, st['key'], st['attempts'])
            if not progress:
                break
        res['pending'] = len(self.store.pending_steps(ref))
        if res['pending']:
            log.warning('заказ %s: ждут повторного запуска шагов: %d (их дожмёт сверка)', ref, res['pending'])
        return res

    # ================================================================== сверка (по расписанию)
    def reconcile(self) -> Dict[str, int]:
        """1) спрашивает у провайдера про счета, по которым долго нет оплаты (вдруг вебхук потерялся);
        2) повторяет отложенные шаги."""
        polled = recovered = 0
        acc = {'done': 0, 'pending': 0, 'dead': 0}
        for o in self.store.awaiting(self.cfg.poll_after_s):
            try:
                inv = self.pay.get_invoice(o['invoice_id'])
            except (Unavailable, ApiError) as exc:
                log.warning('сверка: счёт %s не проверен: %s', o['invoice_id'], exc)
                continue
            polled += 1
            if inv.get('status') == 'paid':
                pay = inv.get('payment') or {}
                data = {'invoice_id': inv['id'], 'order_ref': o['ref'], 'amount_kop': inv.get('amount_kop'),
                        'currency': inv.get('currency'), 'payment_id': pay.get('id'), 'receipt': pay.get('receipt')}
                _, body = self._process_event(f'poll_{inv["id"]}', 'payment.succeeded', o['ref'], data,
                                              verified=False, acc=acc)
                recovered += body.get('status') == 'paid'
        last = self.run_pending()
        return {'polled': polled, 'recovered': recovered, 'done': acc['done'] + last['done'],
                'pending': last['pending'], 'dead': acc['dead'] + last['dead']}

    # ================================================================== шаги
    @staticmethod
    def _need(o: dict, *cols: str) -> None:
        if not all(o.get(c) for c in cols):
            raise Wait(', '.join(cols))

    def _stage(self, o: dict, stage: str, fields: Optional[dict] = None) -> None:
        """Меняет стадию. Если CRM не принимает переход (менеджер мог сдвинуть сделку вручную), не спорим и не ломаем цепочку."""
        try:
            self.crm.set_stage(o['crm_deal_id'], stage, fields)
        except ApiError as exc:
            if exc.status != 409:
                raise
            current = self.crm.get_deal(o['crm_deal_id'])['stage']
            if current != stage:
                log.warning('заказ %s: CRM не принимает переход в «%s» (сейчас «%s»), стадия не тронута, поля сохранены',
                            o['ref'], stage, current)
                if fields:
                    self.crm.set_stage(o['crm_deal_id'], current, fields)

    def _s_crm_deal(self, o, key, p):
        deal = self.crm.create_deal(o['ref'], f'{o["ref"]} · {o["title"]}', o['amount_kop'],
                                    {'name': o['name'], 'email': o['email']})
        self.store.update_order(o['ref'], crm_deal_id=deal['id'])

    def _s_invoice(self, o, key, p):
        inv = self.pay.create_invoice(o['ref'], o['amount_kop'], o['title'], o['email'])
        self.store.update_order(o['ref'], invoice_id=inv['id'], pay_url=inv['pay_url'])
        self.store.set_status(o['ref'], 'AWAITING_PAYMENT', ('NEW',))

    def _s_crm_awaiting(self, o, key, p):
        self._need(o, 'crm_deal_id', 'pay_url')
        self._stage(o, 'AWAITING_PAYMENT', {'invoice_id': o['invoice_id'], 'pay_url': o['pay_url']})
        self.crm.comment(o['crm_deal_id'], f'{o["ref"]}:{key}', f'Выставлен счёт {o["invoice_id"]} на {rub(o["amount_kop"])}.')

    def _s_notify_link(self, o, key, p):
        self._need(o, 'pay_url')
        self.notifier.send('email', o['email'], f'Заказ {o["ref"]}: ссылка на оплату\n\nЗдравствуйте, {o["name"]}!\n'
                           f'{o["title"]}: {rub(o["amount_kop"])}.\nОплатить: {o["pay_url"]}')

    def _s_grant_access(self, o, key, p):
        token = hmac.new(self.cfg.access_secret.encode(), o['ref'].encode(), hashlib.sha256).hexdigest()[:16]
        self.store.update_order(o['ref'], access_url=f'{self.cfg.access_base}/{token}')      # одна ссылка на заказ, повтор безопасен
        self.store.set_status(o['ref'], 'ACCESS_GRANTED', ('PAID',))

    def _s_notify_paid(self, o, key, p):
        self._need(o, 'access_url')
        self.notifier.send('email', o['email'], f'Заказ {o["ref"]}: оплата получена\n\nСпасибо, {o["name"]}! '
                           f'Оплата {rub(o["amount_kop"])} получена.\nЧек № {o["receipt"]} (учебный, не фискальный).\n'
                           f'Ваш доступ: {o["access_url"]}')

    def _s_crm_paid(self, o, key, p):
        self._need(o, 'crm_deal_id')
        self._stage(o, 'PAID', {'receipt': o['receipt']})
        self.crm.comment(o['crm_deal_id'], f'{o["ref"]}:{key}',
                         f'Оплата получена: {rub(o["amount_kop"])}, платёж {o["payment_id"]}, чек № {o["receipt"]}.')

    def _s_crm_done(self, o, key, p):
        self._need(o, 'crm_deal_id', 'access_url')
        self._stage(o, 'ACCESS_GRANTED', {'access_url': o['access_url']})
        self.crm.comment(o['crm_deal_id'], f'{o["ref"]}:{key}', 'Доступ открыт.')

    def _s_notify_manager(self, o, key, p):
        self._need(o, 'access_url')
        self.notifier.send('telegram', self.cfg.manager_chat, f'Оплачен заказ {o["ref"]}\n{o["title"]}, '
                           f'{rub(o["amount_kop"])}. Клиент: {o["name"]} <{o["email"]}>. Доступ выдан автоматически.')

    def _s_crm_failed(self, o, key, p):
        self._need(o, 'crm_deal_id')
        self._stage(o, 'PAYMENT_FAILED')
        self.crm.comment(o['crm_deal_id'], f'{o["ref"]}:{key}',
                         f'Платёж отклонён: {REASON_RU.get(p.get("reason"), p.get("reason") or "причина не указана")}.')

    def _s_notify_failed(self, o, key, p):
        self._need(o, 'pay_url')
        self.notifier.send('email', o['email'], f'Заказ {o["ref"]}: платёж не прошёл\n\nЗдравствуйте, {o["name"]}! '
                           f'Платёж отклонён ({REASON_RU.get(p.get("reason"), p.get("reason") or "причина не указана")}).\n'
                           f'Попробовать ещё раз можно по той же ссылке: {o["pay_url"]}')

    def _s_crm_alert(self, o, key, p):
        self._need(o, 'crm_deal_id')
        self.crm.comment(o['crm_deal_id'], f'{o["ref"]}:{key}', p['text'])

    def _s_notify_alert(self, o, key, p):
        self.notifier.send('telegram', self.cfg.manager_chat, p['text'])
