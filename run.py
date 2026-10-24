#!/usr/bin/env python3
"""Сквозной тестовый запуск: поднимает тестовые CRM, платёжный сервис, почту и сайт, запускает сервис интеграции
и проходит восемь сценариев. Каждый сценарий заканчивается проверками; если хоть одна не прошла, код выхода 1.

    python run.py            прогон сценариев, журнал в out/run.log
    python run.py --serve    после прогона оставить стенд включённым и открыть сайт в браузере
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

import requests

from mocks.pay import sign
from stand import SECRET, Stand

OUT = pathlib.Path(__file__).parent / 'out'
demo, site, sim = logging.getLogger('demo'), logging.getLogger('site'), logging.getLogger('sim')
CHECKS = 0


def check(cond: bool, text: str) -> None:
    global CHECKS
    if not cond:
        demo.error('✗ ПРОВЕРКА НЕ ПРОЙДЕНА: %s', text)
        raise AssertionError(text)
    CHECKS += 1
    demo.info('✓ %s', text)


def head(title: str) -> None:
    demo.info('═══ %s ═══', title)


def click(pay_url: str, outcome: str) -> None:
    """Клиент нажимает кнопку на странице оплаты (обычный POST формы, как в браузере)."""
    r = requests.post(pay_url, data={'outcome': outcome}, allow_redirects=False, timeout=15)
    assert r.status_code == 303, r.status_code


def forged_event(st: Stand, ref: str, **over) -> dict:
    o = st.svc.store.order(ref)
    data = {'invoice_id': o['invoice_id'], 'payment_id': 'pay_fake', 'order_ref': ref, 'amount_kop': o['amount_kop'],
            'currency': 'RUB', 'receipt': {'number': 'FAKE-0001'}}
    return {'id': over.pop('id', 'evt_forged'), 'type': over.pop('type', 'payment.succeeded'),
            'created_at': int(time.time()), 'data': {**data, **over}}


def cut(deal: dict) -> list:
    return [c['text'].replace(' ', ' ') for c in deal['timeline']]


def money_ok(n: int) -> str:
    return f'{n:,}'.replace(',', ' ')


# ====================================================================== сценарии
def s1_happy(st: Stand) -> None:
    head('Сценарий 1. Обычная оплата: заявка → сделка → счёт → оплата → доступ')
    site.info('клиент Анна Ковалёва заполняет форму: «Мини-курс «Excel для учёта»» (заявка lead-1001)')
    r = st.submit('lead-1001', 'Анна Ковалёва', 'anna@example.com', 'excel-mini')
    check(r.status_code == 201 and bool(r.json()['pay_url']), 'сайт получил ссылку на оплату (HTTP 201)')
    ref, pay_url = r.json()['order_id'], r.json()['pay_url']
    check(st.svc.store.order(ref)['status'] == 'AWAITING_PAYMENT', 'заказ ждёт оплаты, в CRM одна сделка')
    site.info('клиент открывает страницу оплаты и нажимает «Оплатить»')
    click(pay_url, 'success')
    o = st.svc.store.order(ref)
    deal = st.deal(o['crm_deal_id'])
    check(o['status'] == 'ACCESS_GRANTED' and st.provider.last_codes == [200], 'вебхук принят с первой попытки, заказ оплачен')
    check(deal['stage'] == 'ACCESS_GRANTED', 'сделка в CRM дошла до стадии «Доступ выдан»')
    check(len(deal['timeline']) == 3, 'история сделки: счёт выставлен, оплата получена, доступ открыт')
    mails = [m for m in st.messages() if m['to'] == 'anna@example.com']
    check(len(mails) == 2 and len(st.messages()) == 3, 'клиент получил 2 письма (ссылка, подтверждение с доступом), менеджер 1 сообщение')


def s2_duplicates(st: Stand) -> None:
    head('Сценарий 2. Двойной клик, чужие данные под тем же номером и подмена цены')
    deals0, invoices0 = len(st.deals()), len(st.provider.invoices)
    site.info('клиент Борис Лебедев дважды нажимает «Оформить заказ» (в обоих запросах один lead-1002)')
    r1 = st.submit('lead-1002', 'Борис Лебедев', 'boris@example.com', 'consult-60')
    r2 = st.submit('lead-1002', 'Борис Лебедев', 'boris@example.com', 'consult-60')
    check((r1.status_code, r2.status_code) == (201, 200) and r2.json()['duplicate'], 'второй запрос получил тот же заказ (HTTP 200), дубль не создан')
    check(r1.json()['pay_url'] == r2.json()['pay_url'], 'ссылка на оплату одна и та же')
    check(len(st.deals()) == deals0 + 1 and len(st.provider.invoices) == invoices0 + 1, 'в CRM одна новая сделка, у провайдера один новый счёт')
    r3 = st.submit('lead-1002', 'Борис Лебедев', 'boris@example.com', 'audit')
    check(r3.status_code == 409, 'тот же номер заявки с другим продуктом отклонён (HTTP 409)')
    sim.info('скрипт отправляет заявку lead-1003 с ценой 1 ₽ в теле запроса (amount_kop=100)')
    r4 = st.submit('lead-1003', 'Вера Тестова', 'vera@example.com', 'consult-60', amount_kop=100)
    o = st.svc.store.order(r4.json()['order_id'])
    check(o['amount_kop'] == 350_000 and st.provider.invoices[o['invoice_id']]['amount_kop'] == 350_000,
          'сумма взята из каталога на сервере (3 500 ₽), цена из формы проигнорирована')
    r5 = st.submit('lead-1004', ' ', 'не-почта', 'unknown')
    check(r5.status_code == 422 and set(r5.json()['fields']) == {'name', 'email', 'product'}, 'пустое имя, неверная почта и неизвестный продукт отклонены (HTTP 422)')


def s3_webhook_repeat(st: Stand) -> None:
    head('Сценарий 3. Провайдер повторно присылает уже обработанный вебхук')
    ref = 'ORD-0002'
    site.info('клиент Борис оплачивает заказ')
    click(st.svc.store.order(ref)['pay_url'], 'success')
    event = st.provider.events[-1]
    deal = st.deal(st.svc.store.order(ref)['crm_deal_id'])
    before = (len(deal['timeline']), len(st.messages()))
    check(deal['stage'] == 'ACCESS_GRANTED', 'первая доставка обработана: сделка в стадии «Доступ выдан»')
    for n in (1, 2):
        st.provider.deliver(event, retry=False)
        check('duplicate' in st.provider.last_body, f'повторная доставка №{n}: сервис ответил 200 «duplicate»')
    deal = st.deal(deal['id'])
    check((len(deal['timeline']), len(st.messages())) == before, 'комментариев в CRM и писем не прибавилось')


def s4_forgery(st: Stand) -> None:
    head('Сценарий 4. Поддельные вебхуки и сумма, которая не сходится с заказом')
    ref = 'ORD-0003'                                           # заказ Веры, он не оплачен
    event = forged_event(st, ref)
    raw = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode()
    now = int(time.time())
    sim.info('злоумышленник пытается «оплатить» чужой заказ вебхуком без подписи, с чужой подписью, со старой подписью и с изменённым телом')
    cases = [('без подписи', None, raw),
             ('подпись чужим секретом', sign('guessed-secret', raw, now), raw),
             ('старая подпись (час назад)', sign(SECRET, raw, now - 3600), raw),
             ('подпись от другого тела', sign(SECRET, raw, now), raw.replace(b'"FAKE-0001"', b'"FAKE-0002"'))]
    for title, sig, body in cases:
        check(st.post_webhook(body, sig).status_code == 401, f'{title}: отказ (HTTP 401)')
    check(st.svc.store.order(ref)['status'] == 'AWAITING_PAYMENT' and st.svc.store.event_outcome('evt_forged') is None,
          'заказ остался неоплаченным, подделки в журнал событий не попали')
    sim.info('провайдер присылает настоящую подпись, но сумма 1 ₽ вместо 3 500 ₽ (ошибка на стороне провайдера или атака)')
    wrong = forged_event(st, ref, id='evt_9001', amount_kop=100, payment_id='pay_9001')
    msgs0 = len(st.messages())
    st.provider.deliver(wrong, retry=False)
    o = st.svc.store.order(ref)
    check('rejected:amount_mismatch' in st.provider.last_body and o['status'] == 'AWAITING_PAYMENT', 'сумма не сошлась: статус заказа не изменён, доступ не выдан')
    deal = st.deal(o['crm_deal_id'])
    check(deal['timeline'][-1]['text'].startswith('⚠') and len(st.messages()) == msgs0 + 1, 'в сделку записано предупреждение, менеджеру ушло сообщение')


def s5_failed_then_paid(st: Stand) -> None:
    head('Сценарий 5. Платёж отклонён, клиент повторяет и платит')
    site.info('клиентка Галина Орлова оформляет «Консультацию 60 минут» (lead-1005)')
    r = st.submit('lead-1005', 'Галина Орлова', 'galina@example.com', 'consult-60')
    ref, pay_url = r.json()['order_id'], r.json()['pay_url']
    site.info('на странице оплаты банк отклоняет платёж')
    click(pay_url, 'fail')
    o = st.svc.store.order(ref)
    check(o['status'] == 'PAYMENT_FAILED' and st.deal(o['crm_deal_id'])['stage'] == 'PAYMENT_FAILED', 'заказ и сделка в статусе «Оплата не прошла»')
    check(any('платёж не прошёл' in m['text'] for m in st.messages() if m['to'] == 'galina@example.com'), 'клиентка получила письмо со ссылкой для повторной попытки')
    site.info('Галина повторяет оплату по той же ссылке')
    click(pay_url, 'success')
    o = st.svc.store.order(ref)
    check(o['status'] == 'ACCESS_GRANTED' and st.deal(o['crm_deal_id'])['stage'] == 'ACCESS_GRANTED', 'после успешной оплаты сделка дошла до «Доступ выдан»')
    sim.info('через минуту до сервиса доходит запоздавшее уведомление о первом отказе (новый номер события)')
    st.provider.deliver(forged_event(st, ref, id='evt_late', type='payment.failed', payment_id='pay_0004', reason='insufficient_funds'), retry=False)
    check('ignored:late_failed_after_paid' in st.provider.last_body, 'запоздавший отказ проигнорирован')
    check(st.svc.store.order(ref)['status'] == 'ACCESS_GRANTED' and st.deal(o['crm_deal_id'])['stage'] == 'ACCESS_GRANTED', 'оплаченный заказ не откатился назад')
    site.info('клиент Игорь Смирнов (lead-1006) получил отказ банка и пока не повторил оплату')
    r = st.submit('lead-1006', 'Игорь Смирнов', 'igor@example.com', 'excel-mini')
    click(r.json()['pay_url'], 'fail')
    check(st.svc.store.order(r.json()['order_id'])['status'] == 'PAYMENT_FAILED', 'заказ Игоря ждёт повторной попытки')


def s6_crm_down(st: Stand) -> None:
    head('Сценарий 6. CRM недоступна в момент оплаты')
    site.info('клиент Денис Громов оформляет «Мини-курс «Excel для учёта»» (lead-1007)')
    r = st.submit('lead-1007', 'Денис Громов', 'denis@example.com', 'excel-mini')
    ref, pay_url = r.json()['order_id'], r.json()['pay_url']
    st.crm_down(True)
    sim.info('CRM перестаёт отвечать (ошибка 503)')
    site.info('Денис оплачивает заказ')
    click(pay_url, 'success')
    o = st.svc.store.order(ref)
    check(st.provider.last_codes == [200] and o['status'] == 'ACCESS_GRANTED', 'вебхук принят (HTTP 200), оплата записана у нас, доступ выдан')
    mails = [m['text'] for m in st.messages() if m['to'] == 'denis@example.com']
    check(len(mails) == 2 and 'Ваш доступ:' in mails[-1], 'Денис сразу получил письмо с доступом, CRM для этого не нужна')
    pending = st.svc.store.pending_steps(ref)
    check(sorted(s['key'] for s in pending) == ['crm_done', 'crm_paid'], 'два шага CRM отложены в очередь, ничего не потеряно')
    st.crm_down(False)
    sim.info('CRM снова отвечает')
    demo.info('запускается сверка (в рабочей системе её запускает планировщик, например cron)')
    res = st.svc.reconcile()
    deal = st.deal(o['crm_deal_id'])
    check(res['pending'] == 0 and deal['stage'] == 'ACCESS_GRANTED', 'сверка догнала CRM: сделка в стадии «Доступ выдан»')
    check(cut(deal)[-2].startswith('Оплата получена') and cut(deal)[-1] == 'Доступ открыт.', 'записи в истории сделки идут в правильном порядке')
    check(len([m for m in st.messages() if m['to'] == 'denis@example.com']) == 2, 'повторных писем клиенту нет')


def s7_service_restart(st: Stand) -> None:
    head('Сценарий 7. Сервис перезапускается, провайдер повторяет доставку')
    site.info('клиентка Елена Фролова оформляет «Консультацию 60 минут» (lead-1008)')
    r = st.submit('lead-1008', 'Елена Фролова', 'elena@example.com', 'consult-60')
    ref, pay_url = r.json()['order_id'], r.json()['pay_url']
    st.gate.fail = 2
    sim.info('шлюз перед сервисом два раза отвечает 502 (имитация перезапуска)')
    site.info('клиентка нажимает «Оплатить»')
    click(pay_url, 'success')
    check(st.provider.last_codes == [502, 502, 200], 'провайдер получил 502, 502 и повторил доставку до ответа 200')
    o = st.svc.store.order(ref)
    check(o['status'] == 'ACCESS_GRANTED' and st.deal(o['crm_deal_id'])['stage'] == 'ACCESS_GRANTED', 'заказ обработан ровно один раз')
    check(len([m for m in st.messages() if m['to'] == 'elena@example.com']) == 2, 'клиентка получила по одному письму каждого вида')


def s8_lost_webhook(st: Stand) -> None:
    head('Сценарий 8. Вебхук потерялся совсем: сверка спрашивает провайдера')
    site.info('клиент Фёдор Никитин оформляет «Аудит рабочих процессов» (lead-1009)')
    r = st.submit('lead-1009', 'Фёдор Никитин', 'fedor@example.com', 'audit')
    ref = r.json()['order_id']
    sim.info('Фёдор оплатил, но уведомление до сервиса не дошло ни разу')
    event, _ = st.provider.complete(st.invoice_id(ref), 'success', deliver=False)
    check(st.svc.store.order(ref)['status'] == 'AWAITING_PAYMENT', 'сервис пока не знает об оплате')
    demo.info('запускается сверка')
    res = st.svc.reconcile()
    o = st.svc.store.order(ref)
    check(res['recovered'] == 1 and o['status'] == 'ACCESS_GRANTED', 'сверка по счетам нашла оплату и запустила обычную обработку')
    check(st.deal(o['crm_deal_id'])['stage'] == 'ACCESS_GRANTED', 'сделка дошла до «Доступ выдан»')
    msgs0 = len(st.messages())
    sim.info('спустя время провайдер всё же доставляет потерянное уведомление')
    st.provider.deliver(event, retry=False)
    check('ignored:already_paid' in st.provider.last_body, 'запоздавшее уведомление распознано как уже учтённая оплата')
    check(len(st.messages()) == msgs0, 'повторных писем и записей нет')


SCENARIOS = [s1_happy, s2_duplicates, s3_webhook_repeat, s4_forgery, s5_failed_then_paid, s6_crm_down,
             s7_service_restart, s8_lost_webhook]


def summary(st: Stand) -> None:
    head('Итог')
    stats = st.svc.store.stats()
    stages: dict = {}
    for d in st.deals():
        stages[d['stage']] = stages.get(d['stage'], 0) + 1
    fmt = lambda d: ', '.join(f'{k}: {v}' for k, v in sorted(d.items())) or '—'
    demo.info('заказов: %d (%s)', sum(stats['orders'].values()), fmt(stats['orders']))
    demo.info('сделок в CRM: %d (%s)', len(st.deals()), fmt(stages))
    demo.info('события провайдера: %s', fmt(stats['events']))
    demo.info('шаги: %s', fmt(stats['steps']))
    msgs = st.messages()
    demo.info('сообщений: %d (клиентам %d, менеджеру %d)', len(msgs), sum(m['channel'] == 'email' for m in msgs),
              sum(m['channel'] == 'telegram' for m in msgs))
    check(stats['steps'].get('pending', 0) == 0 and stats['steps'].get('dead', 0) == 0, 'очередь шагов пуста, «мёртвых» шагов нет')
    demo.info('все проверки пройдены: %d', CHECKS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--serve', action='store_true', help='оставить стенд включённым после прогона')
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors='replace')
    OUT.mkdir(exist_ok=True)
    fmt = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-4s %(message)s', '%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(OUT / 'run.log', 'w', encoding='utf-8')):
        handler.setFormatter(fmt)
        root.addHandler(handler)

    st = Stand()
    demo.info('стенд запущен на этом компьютере: сайт %s, CRM %s, платежи %s, сервис интеграции %s',
              st.site_srv.url, st.crm_srv.url, st.pay_srv.url, st.svc_srv.url)
    demo.info('всё учебное: чужие сервисы не затрагиваются, деньги и письма настоящими не бывают')
    try:
        for scenario in SCENARIOS:
            scenario(st)
        summary(st)
        if args.serve:
            demo.info('стенд включён. Форма заказа: %s | доска сделок CRM: %s | остановить: Ctrl+C', st.site_srv.url, st.crm_srv.url)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    except AssertionError:
        return 1
    finally:
        st.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
