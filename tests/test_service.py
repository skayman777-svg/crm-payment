"""Сквозные проверки интеграции на локальном стенде: заявка, вебхуки, сбои, сверка."""
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import requests

from integration.config import Settings
from tests.base import StandCase


class LeadTests(StandCase):
    def test_new_lead_creates_order_deal_invoice_and_mail(self):
        r = self.st.submit('lead-1', 'Анна Ковалёва', 'anna@example.com', 'excel-mini')
        self.assertEqual(r.status_code, 201)
        ref = r.json()['order_id']
        o = self.order(ref)
        self.assertEqual((o['status'], o['amount_kop']), ('AWAITING_PAYMENT', 590_000))
        self.assertEqual(self.deal_of(ref)['stage'], 'AWAITING_PAYMENT')
        self.assertEqual(self.deal_of(ref)['fields']['pay_url'], o['pay_url'])
        self.assertEqual([m['channel'] for m in self.st.messages()], ['email'])
        self.assertIn(o['pay_url'], self.st.messages()[0]['text'])

    def test_repeated_lead_returns_the_same_order(self):
        a = self.st.submit('lead-1', 'Анна', 'anna@example.com', 'audit')
        b = self.st.submit('lead-1', 'Анна', 'anna@example.com', 'audit')
        self.assertEqual((a.status_code, b.status_code, b.json()['duplicate']), (201, 200, True))
        self.assertEqual(a.json()['pay_url'], b.json()['pay_url'])
        self.assertEqual((len(self.st.deals()), len(self.st.provider.invoices), len(self.st.messages())), (1, 1, 1))

    def test_same_lead_id_with_other_data_is_conflict(self):
        self.st.submit('lead-1', 'Анна', 'anna@example.com', 'audit')
        for other in (('Анна', 'anna@example.com', 'consult-60'), ('Анна', 'boris@example.com', 'audit'), ('Борис', 'anna@example.com', 'audit')):
            self.assertEqual(self.st.submit('lead-1', *other).status_code, 409, other)
        self.assertEqual(len(self.st.deals()), 1)

    def test_price_comes_from_catalog_not_from_the_form(self):
        r = self.st.submit('lead-1', 'Анна', 'anna@example.com', 'audit', amount_kop=1, amount=1, price=1)
        o = self.order(r.json()['order_id'])
        self.assertEqual(o['amount_kop'], 1_200_000)
        self.assertEqual(self.st.provider.invoices[o['invoice_id']]['amount_kop'], 1_200_000)

    def test_validation_errors(self):
        bad = [({'lead_id': '', 'name': 'А', 'email': 'a@example.com', 'product': 'audit'}, {'lead_id'}),
               ({'lead_id': 'x' * 65, 'name': 'А', 'email': 'a@example.com', 'product': 'audit'}, {'lead_id'}),
               ({'lead_id': 'l', 'name': ' ', 'email': 'a@example.com', 'product': 'audit'}, {'name'}),
               ({'lead_id': 'l', 'name': 'А' * 101, 'email': 'a@example.com', 'product': 'audit'}, {'name'}),
               ({'lead_id': 'l', 'name': 'А', 'email': 'no-at-sign', 'product': 'audit'}, {'email'}),
               ({'lead_id': 'l', 'name': 'А', 'email': 'a@example.com', 'product': 'free-money'}, {'product'}),
               ({'lead_id': 5, 'name': None, 'email': ['a@example.com'], 'product': {}}, {'lead_id', 'name', 'email', 'product'})]
        for body, fields in bad:
            r = requests.post(f'{self.st.svc_srv.url}/leads', json=body, headers={'Authorization': 'Bearer site-demo-token'})
            self.assertEqual((r.status_code, set(r.json()['fields'])), (422, fields), body)
        self.assertEqual(self.st.deals(), [])

    def test_endpoint_requires_the_site_token_and_json(self):
        url = f'{self.st.svc_srv.url}/leads'
        body = {'lead_id': 'l', 'name': 'А', 'email': 'a@example.com', 'product': 'audit'}
        self.assertEqual(requests.post(url, json=body).status_code, 401)
        self.assertEqual(requests.post(url, json=body, headers={'Authorization': 'Bearer wrong'}).status_code, 401)
        self.assertEqual(requests.post(url, data='not json', headers={'Authorization': 'Bearer site-demo-token'}).status_code, 400)
        self.assertEqual(requests.post(url, json=[1], headers={'Authorization': 'Bearer site-demo-token'}).status_code, 400)

    def test_health_does_not_leak_business_data(self):
        self.place()
        self.assertEqual(requests.get(f'{self.st.svc_srv.url}/health').json(), {'status': 'ok'})
        self.assertEqual(self.st.svc.store.stats()['steps'], {'done': 4})

    def test_oversized_bodies_are_refused(self):
        big = b'x' * 70_000
        self.assertEqual(requests.post(f'{self.st.svc_srv.url}/leads', data=big,
                                       headers={'Authorization': 'Bearer site-demo-token'}).status_code, 413)
        self.assertEqual(self.st.post_webhook(big, 't=1,v1=00').status_code, 413)


class PaymentTests(StandCase):
    def test_successful_payment_runs_the_whole_chain(self):
        ref, url = self.place()
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        o = self.order(ref)
        self.assertEqual((o['status'], o['receipt'], self.st.provider.last_codes), ('ACCESS_GRANTED', 'DEMO-000001', [200]))
        self.assertEqual(self.deal_of(ref)['stage'], 'ACCESS_GRANTED')
        self.assertEqual(self.texts(ref)[1:], [f'Оплата получена: 5 900 ₽, платёж pay_0001, чек № DEMO-000001.', 'Доступ открыт.'])
        client_mails = self.mails('anna@example.com')
        self.assertIn(o['access_url'], client_mails[-1]['text'])
        self.assertEqual(self.mails('@demo_manager')[0]['channel'], 'telegram')

    def test_access_link_is_personal_and_stable(self):
        a, url_a = self.place('l1', email='a@example.com')
        b, url_b = self.place('l2', email='b@example.com')
        for url in (url_a, url_b):
            requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertNotEqual(self.order(a)['access_url'], self.order(b)['access_url'])
        before = self.order(a)['access_url']
        self.st.svc._s_grant_access(self.order(a), 'grant_access', {})      # повторный запуск шага
        self.assertEqual(self.order(a)['access_url'], before)

    def test_repeated_webhook_changes_nothing(self):
        ref, _ = self.place()
        evt = self.event(ref)
        self.assertEqual(self.send(evt).json()['status'], 'paid')
        snapshot = (len(self.st.messages()), self.texts(ref), self.st.svc.store.stats())
        for _ in range(3):
            r = self.send(evt)
            self.assertEqual((r.status_code, r.json()['status']), (200, 'duplicate'))
        self.assertEqual((len(self.st.messages()), self.texts(ref), self.st.svc.store.stats()), snapshot)

    def test_parallel_duplicates_are_processed_once(self):
        ref, _ = self.place()
        raw, sig = self.signed(self.event(ref))
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.st.post_webhook(raw, sig), range(8)))
        statuses = sorted(r.json()['status'] for r in results)
        self.assertEqual(statuses, ['duplicate'] * 7 + ['paid'])
        self.assertEqual((len(self.mails('anna@example.com')), len(self.mails('@demo_manager')), len(self.texts(ref))), (2, 1, 3))

    def test_failed_payment_then_success(self):
        ref, url = self.place()
        requests.post(url, data={'outcome': 'fail'}, allow_redirects=False)
        self.assertEqual((self.order(ref)['status'], self.deal_of(ref)['stage']), ('PAYMENT_FAILED', 'PAYMENT_FAILED'))
        self.assertIn('недостаточно средств', self.texts(ref)[-1])
        self.assertIn('платёж не прошёл', self.mails('anna@example.com')[-1]['text'])
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertEqual((self.order(ref)['status'], self.deal_of(ref)['stage']), ('ACCESS_GRANTED', 'ACCESS_GRANTED'))

    def test_second_failure_is_recorded_separately(self):
        ref, url = self.place()
        for _ in range(2):
            requests.post(url, data={'outcome': 'fail'}, allow_redirects=False)
        self.assertEqual(sum('Платёж отклонён' in t for t in self.texts(ref)), 2)

    def test_late_failure_after_payment_is_ignored(self):
        ref, url = self.place()
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        r = self.send(self.event(ref, id='evt_late', type='payment.failed', reason='card_declined'))
        self.assertEqual(r.json()['status'], 'ignored:late_failed_after_paid')
        self.assertEqual((self.order(ref)['status'], self.deal_of(ref)['stage']), ('ACCESS_GRANTED', 'ACCESS_GRANTED'))

    def test_amount_mismatch_is_rejected_and_reported(self):
        ref, _ = self.place()
        r = self.send(self.event(ref, amount_kop=100))
        self.assertEqual(r.json()['status'], 'rejected:amount_mismatch')
        self.assertEqual(self.order(ref)['status'], 'AWAITING_PAYMENT')
        self.assertTrue(self.texts(ref)[-1].startswith('⚠'))
        self.assertIn('сумма 1 ₽', self.mails('@demo_manager')[-1]['text'].replace('\u00a0', ' '))
        self.assertEqual(len(self.mails('anna@example.com')), 1)            # клиенту про аномалию не пишем
        # настоящее уведомление после этого всё ещё принимается
        self.assertEqual(self.send(self.event(ref, id='evt_x2')).json()['status'], 'paid')

    def test_garbage_amount_in_signed_event_is_rejected_not_crashing(self):
        ref, _ = self.place()
        for bad in ('abc', None, [1], {'a': 1}, 590000.5):
            r = self.send(self.event(ref, id=f'evt_{bad!r}', amount_kop=bad))
            self.assertEqual((r.status_code, r.json()['status']), (200, 'rejected:amount_mismatch'), bad)
        self.assertEqual(self.order(ref)['status'], 'AWAITING_PAYMENT')

    def test_currency_mismatch_is_rejected(self):
        ref, _ = self.place()
        self.assertEqual(self.send(self.event(ref, currency='USD')).json()['status'], 'rejected:amount_mismatch')
        self.assertIn('(USD)', self.mails('@demo_manager')[-1]['text'])

    def test_second_payment_for_paid_invoice_needs_review(self):
        ref, url = self.place()
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        r = self.send(self.event(ref, id='evt_dup', payment_id='pay_other'))
        self.assertEqual(r.json()['status'], 'needs_review:second_payment')
        self.assertIn('второй платёж', self.mails('@demo_manager')[-1]['text'])
        self.assertEqual(self.order(ref)['payment_id'], 'pay_0001')           # исходный платёж не перезаписан

    def test_unknown_order_invoice_and_event_type_are_ignored(self):
        ref, _ = self.place()
        self.assertEqual(self.send(self.event(ref, order_ref='ORD-9999')).json(), {'status': 'ignored', 'reason': 'unknown_order'})
        self.assertEqual(self.send(self.event(ref, id='e2', invoice_id='inv_other')).json()['status'], 'ignored:other_invoice')
        self.assertEqual(self.send(self.event(ref, id='e3', type='refund.created')).json()['status'], 'ignored:unknown_type')
        self.assertEqual(self.order(ref)['status'], 'AWAITING_PAYMENT')

    def test_forged_webhooks_are_refused(self):
        ref, _ = self.place()
        now = int(time.time())
        evt = self.event(ref)
        raw, good = self.signed(evt)
        cases = {'no signature': (raw, None), 'wrong secret': self.signed(evt, secret='guess'),
                 'stale': self.signed(evt, ts=now - 3600), 'future': self.signed(evt, ts=now + 3600),
                 'tampered': (raw.replace(b'DEMO-X1', b'EVIL'), good)}
        for name, (body, sig) in cases.items():
            self.assertEqual(self.st.post_webhook(body, sig).status_code, 401, name)
        self.assertEqual((self.order(ref)['status'], self.st.svc.store.event_outcome('evt_x1')), ('AWAITING_PAYMENT', None))

    def test_signed_but_malformed_payload_is_bad_request(self):
        for body in (b'not json', b'[1, 2]', b'{"id": "e"}', b'{"id":"e","type":"t","data":{}}', b'{"id":"e","type":"t","data":5}'):
            raw, sig = self.signed(body)
            self.assertEqual(self.st.post_webhook(raw, sig).status_code, 400, body)

    def test_provider_retries_when_service_is_restarting(self):
        ref, url = self.place()
        self.st.gate.fail = 2
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertEqual(self.st.provider.last_codes, [502, 502, 200])
        self.assertEqual((self.order(ref)['status'], len(self.mails('anna@example.com'))), ('ACCESS_GRANTED', 2))


class ResilienceTests(StandCase):
    def test_crm_down_at_payment_time_keeps_the_customer_flow_alive(self):
        ref, url = self.place()
        self.st.crm_down(True)
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertEqual(self.st.provider.last_codes, [200])                  # провайдеру отвечаем «принято»
        self.assertIn('Ваш доступ', self.mails('anna@example.com')[-1]['text'])
        self.assertEqual(sorted(s['key'] for s in self.st.svc.store.pending_steps(ref)), ['crm_done', 'crm_paid'])
        self.st.crm_down(False)
        res = self.st.svc.reconcile()
        self.assertEqual((res['pending'], res['dead']), (0, 0))
        self.assertEqual(self.texts(ref)[1:], ['Оплата получена: 5 900 ₽, платёж pay_0001, чек № DEMO-000001.', 'Доступ открыт.'])
        self.assertEqual(len(self.mails('anna@example.com')), 2)              # письма не продублированы

    def test_dead_system_is_not_hammered_within_one_run(self):
        ref, url = self.place()
        self.st.crm_down(True)
        before = self.st.crm_app.crm_state.requests
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        # три попытки на первый CRM-шаг; второй CRM-шаг в этом запуске не пробуем вовсе
        self.assertEqual(self.st.crm_app.crm_state.requests - before, 3)

    def test_crm_down_from_the_start_orders_still_complete_in_right_order(self):
        self.st.crm_down(True)
        r = self.st.submit('lead-1', 'Анна', 'anna@example.com', 'excel-mini')
        self.assertEqual(r.status_code, 201)                                  # ссылка на оплату есть и без CRM
        ref, url = r.json()['order_id'], r.json()['pay_url']
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.st.crm_down(False)
        self.st.svc.reconcile()
        deal = self.deal_of(ref)
        self.assertEqual(deal['stage'], 'ACCESS_GRANTED')
        self.assertEqual([t.split(':')[0].split(' на ')[0] for t in self.texts(ref)],
                         ['Выставлен счёт inv_0001', 'Оплата получена', 'Доступ открыт.'])
        self.assertEqual(len(self.st.deals()), 1)

    def test_payment_service_down_at_lead_time(self):
        self.st.provider.fail = 10 ** 6
        r = self.st.submit('lead-1', 'Анна', 'anna@example.com', 'excel-mini')
        self.assertEqual((r.status_code, r.json()['pay_url'], r.json()['status']), (202, None, 'NEW'))
        ref = r.json()['order_id']
        # пока счёта нет, уведомление об оплате этого заказа принимать рано: провайдер должен повторить позже
        resp = self.send(self.event(ref, invoice_id='inv_0001'))
        self.assertEqual((resp.status_code, resp.headers.get('Retry-After')), (503, '30'))
        self.assertIsNone(self.st.svc.store.event_outcome('evt_x1'))
        self.st.provider.fail = 0
        self.st.svc.reconcile()
        o = self.order(ref)
        self.assertEqual((o['status'], self.deal_of(ref)['stage']), ('AWAITING_PAYMENT', 'AWAITING_PAYMENT'))
        self.assertIn(o['pay_url'], self.mails('anna@example.com')[0]['text'])
        self.assertEqual(len(self.st.provider.invoices), 1)

    def test_mail_service_down_does_not_block_crm(self):
        self.st.msg_app.msg_state['fail'] = 10 ** 6
        ref, url = self.place()
        self.assertEqual(self.deal_of(ref)['stage'], 'AWAITING_PAYMENT')
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertEqual(self.deal_of(ref)['stage'], 'ACCESS_GRANTED')
        self.assertEqual(self.st.messages(), [])
        self.st.msg_app.msg_state['fail'] = 0
        self.st.svc.reconcile()
        self.assertEqual((len(self.mails('anna@example.com')), len(self.mails('@demo_manager'))), (2, 1))

    def test_hopeless_step_is_marked_dead_and_logged(self):
        # свой стенд: после двух неудачных запусков шаг помечается «мёртвым»
        self.st.stop()
        self.st = self.enter_stand(step_max_attempts=2)
        ref, url = self.place()
        self.st.crm_down(True)
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)       # неудача №1
        with self.assertLogs('svc', 'ERROR') as logs:
            res = self.st.svc.reconcile()                                            # неудача №2: лимит
        self.assertEqual(res['dead'], 1)
        self.assertIn('нужна ручная проверка', logs.output[0])
        self.assertEqual(self.st.svc.store.stats()['steps']['dead'], 1)

    def test_failed_crm_step_holds_back_the_rest_of_the_chain(self):
        from integration.http import ApiError
        ref, url = self.place()
        real, calls = self.st.svc.crm.set_stage, []

        def refuse(deal_id, stage, fields=None):                 # CRM один раз отказывает (4xx) на стадию «Оплачено»
            calls.append(stage)
            if stage == 'PAID' and len(calls) == 1:
                raise ApiError('CRM', 'PATCH', f'/api/deals/{deal_id}', 422, 'отказ')
            return real(deal_id, stage, fields)
        self.st.svc.crm.set_stage = refuse
        requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertEqual(calls, ['PAID'])                         # «Доступ выдан» не пробовали: нельзя обгонять «Оплачено»
        self.assertEqual(self.deal_of(ref)['stage'], 'AWAITING_PAYMENT')
        self.st.svc.reconcile()
        self.assertEqual(calls, ['PAID', 'PAID', 'ACCESS_GRANTED'])
        self.assertEqual(self.deal_of(ref)['stage'], 'ACCESS_GRANTED')
        self.assertEqual(self.texts(ref)[1:], ['Оплата получена: 5 900 ₽, платёж pay_0001, чек № DEMO-000001.', 'Доступ открыт.'])

    def test_dead_crm_step_holds_back_the_chain_until_it_is_revived(self):
        self.st.stop()
        self.st = self.enter_stand(step_max_attempts=1)
        ref, url = self.place()
        self.st.crm_down(True)
        with self.assertLogs('svc', 'ERROR'):
            requests.post(url, data={'outcome': 'success'}, allow_redirects=False)       # «Оплачено» исчерпал попытки
        self.st.crm_down(False)
        res = self.st.svc.reconcile()
        self.assertEqual((res['done'], res['pending'], self.deal_of(ref)['stage']), (0, 1, 'AWAITING_PAYMENT'))   # «Доступ выдан» ждёт
        self.st.svc.store.db.execute("UPDATE steps SET status = 'pending', attempts = 0 WHERE status = 'dead'")   # как в README
        res = self.st.svc.reconcile()
        self.assertEqual((res['done'], res['pending'], res['dead']), (2, 0, 0))
        self.assertEqual(self.deal_of(ref)['stage'], 'ACCESS_GRANTED')
        self.assertEqual(self.texts(ref)[1:], ['Оплата получена: 5 900 ₽, платёж pay_0001, чек № DEMO-000001.', 'Доступ открыт.'])

    def enter_stand(self, **kw):
        from stand import Stand
        st = Stand(delays=(0, 0.01), backoff=0.01, poll_after_s=0, **kw)
        self.addCleanup(st.stop)
        return st

    def test_manual_stage_change_in_crm_does_not_break_the_chain(self):
        ref, url = self.place()
        self.st.crm_app.crm_state.deals[self.order(ref)['crm_deal_id']]['stage'] = 'ACCESS_GRANTED'   # менеджер сдвинул сам
        with self.assertLogs('svc', 'WARNING') as logs:
            requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        deal = self.deal_of(ref)
        self.assertEqual(deal['stage'], 'ACCESS_GRANTED')
        self.assertEqual(deal['fields']['receipt'], 'DEMO-000001')            # поля всё равно сохранены
        self.assertEqual(self.st.svc.store.stats()['steps'], {'done': 9})
        self.assertTrue(any('CRM не принимает переход' in line for line in logs.output))

    def test_unexpected_error_in_one_step_does_not_block_the_others(self):
        ref, url = self.place()
        real = self.st.svc.handlers['notify_paid']
        calls = []

        def broken(o, key, p):
            calls.append(1)
            if len(calls) == 1:
                raise ZeroDivisionError('ошибка в коде шага')
            return real(o, key, p)
        self.st.svc.handlers['notify_paid'] = broken
        with self.assertLogs('svc', 'ERROR') as logs:
            requests.post(url, data={'outcome': 'success'}, allow_redirects=False)
        self.assertIn('неожиданной ошибкой', logs.output[0])
        self.assertEqual(self.st.provider.last_codes, [200])                 # оплата принята, провайдер не мучаем
        self.assertEqual(self.deal_of(ref)['stage'], 'ACCESS_GRANTED')       # шаги CRM выполнены
        pending = self.st.svc.store.pending_steps(ref)
        self.assertEqual([s['key'] for s in pending], ['notify_paid'])
        self.assertIn('ZeroDivisionError', pending[0]['last_error'])
        self.st.svc.reconcile()                                              # после «исправления кода» шаг доходит
        self.assertEqual((self.st.svc.store.pending_steps(ref), len(self.mails('anna@example.com'))), ([], 2))

    def test_invoice_creation_is_idempotent(self):
        ref, _ = self.place()
        o = self.order(ref)
        again = self.st.svc.pay.create_invoice(ref, o['amount_kop'], o['title'], o['email'])
        self.assertEqual((again['id'], len(self.st.provider.invoices)), (o['invoice_id'], 1))

    def test_deal_creation_is_idempotent(self):
        ref, _ = self.place()
        o = self.order(ref)
        again = self.st.svc.crm.create_deal(ref, 'x', o['amount_kop'], {})
        self.assertEqual((again['id'], len(self.st.deals())), (o['crm_deal_id'], 1))

    def test_database_survives_restart(self):
        import os
        import tempfile
        from integration.service import Service
        with tempfile.TemporaryDirectory() as tmp:
            first = self.enter_stand(db_path=os.path.join(tmp, 'orders.db'))
            lead = {'lead_id': 'lead-1', 'name': 'Анна', 'email': 'anna@example.com', 'product': 'audit'}
            ref = first.submit(**lead).json()['order_id']
            again = Service(first.cfg)                      # «перезапуск»: новое подключение к тому же файлу базы
            self.addCleanup(again.store.close)
            self.assertEqual(again.store.order(ref)['status'], 'AWAITING_PAYMENT')
            code, body = again.accept_lead(lead)
            self.assertEqual((code, body['duplicate'], body['order_id']), (200, True, ref))


class ReconcileTests(StandCase):
    def test_lost_webhook_is_recovered_once(self):
        ref, _ = self.place()
        event, _ = self.st.provider.complete(self.st.invoice_id(ref), 'success', deliver=False)
        self.assertEqual(self.order(ref)['status'], 'AWAITING_PAYMENT')
        first = self.st.svc.reconcile()
        self.assertEqual((first['recovered'], self.order(ref)['status']), (1, 'ACCESS_GRANTED'))
        snapshot = (len(self.st.messages()), self.texts(ref))
        second = self.st.svc.reconcile()
        self.assertEqual((second['polled'], second['recovered']), (0, 0))      # оплаченные заказы больше не опрашиваем
        self.st.provider.deliver(event, retry=False)                           # запоздавший вебхук
        self.assertIn('ignored:already_paid', self.st.provider.last_body)
        self.assertEqual((len(self.st.messages()), self.texts(ref)), snapshot)

    def test_unpaid_invoices_are_left_alone(self):
        ref, _ = self.place()
        res = self.st.svc.reconcile()
        self.assertEqual((res['polled'], res['recovered'], self.order(ref)['status']), (1, 0, 'AWAITING_PAYMENT'))

    def test_recent_orders_are_not_polled_before_the_wait_time(self):
        self.st.svc.cfg.poll_after_s = 600
        self.place()
        self.assertEqual(self.st.svc.reconcile()['polled'], 0)

    def test_provider_outage_during_reconcile_is_survived(self):
        ref, _ = self.place()
        self.st.provider.complete(self.st.invoice_id(ref), 'success', deliver=False)
        self.st.provider.fail = 10 ** 6
        with self.assertLogs('svc', 'WARNING'):
            res = self.st.svc.reconcile()
        self.assertEqual((res['polled'], self.order(ref)['status']), (0, 'AWAITING_PAYMENT'))
        self.st.provider.fail = 0
        self.assertEqual(self.st.svc.reconcile()['recovered'], 1)

    def test_reconcile_counts_the_steps_it_ran_while_recovering_a_payment(self):
        ref, _ = self.place()
        self.st.provider.complete(self.st.invoice_id(ref), 'success', deliver=False)
        res = self.st.svc.reconcile()
        self.assertEqual((res['recovered'], res['done'], res['pending'], res['dead']), (1, 5, 0, 0))   # пять шагов после оплаты


class ReconcileDeadStepTests(StandCase):
    stand_kw = {'step_max_attempts': 1}

    def test_step_that_runs_out_of_attempts_while_recovering_is_reported(self):
        ref, _ = self.place()
        self.st.provider.complete(self.st.invoice_id(ref), 'success', deliver=False)
        self.st.crm_down(True)
        with self.assertLogs('svc', 'ERROR'):
            res = self.st.svc.reconcile()
        # «Оплачено» исчерпал попытки при поиске оплаты (это надо посчитать), «Доступ выдан» ждёт его
        self.assertEqual((res['recovered'], res['dead'], res['pending']), (1, 1, 1))


class SiteAndPagesTests(StandCase):
    def test_form_submission_redirects_to_payment_page(self):
        r = requests.post(f'{self.st.site_srv.url}/order', allow_redirects=False,
                          data={'lead_id': 'web-1', 'name': 'Анна', 'email': 'anna@example.com', 'product': 'consult-60'})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers['Location'].startswith(self.st.pay_srv.url + '/pay/'))
        again = requests.post(f'{self.st.site_srv.url}/order', allow_redirects=False,       # двойной клик
                              data={'lead_id': 'web-1', 'name': 'Анна', 'email': 'anna@example.com', 'product': 'consult-60'})
        self.assertEqual((again.status_code, again.headers['Location'], len(self.st.deals())), (303, r.headers['Location'], 1))

    def test_form_errors_and_outage_are_shown_to_the_visitor(self):
        bad = requests.post(f'{self.st.site_srv.url}/order', data={'lead_id': 'w', 'name': '', 'email': 'x', 'product': 'audit'})
        self.assertEqual(bad.status_code, 422)
        self.assertIn('Проверьте данные', bad.text)
        self.st.svc_srv.stop()
        down = requests.post(f'{self.st.site_srv.url}/order', data={'lead_id': 'w2', 'name': 'А', 'email': 'a@example.com', 'product': 'audit'})
        self.assertEqual(down.status_code, 503)
        self.assertIn('временно недоступен', down.text)

    def test_pages_render(self):
        home = requests.get(self.st.site_srv.url).text
        self.assertIn('Школа «Excel для учёта»', home)
        self.assertIn('ТЕСТОВЫЙ СТЕНД', home)
        self.assertEqual(home.count('name="product"'), 3)
        ref, url = self.place()
        self.assertIn(ref, requests.get(url).text)
        self.assertIn(ref, requests.get(self.st.crm_srv.url).text)
        self.assertEqual(requests.get(f'{self.st.pay_srv.url}/pay/inv_9999').status_code, 404)

    def test_visitor_data_is_escaped_on_crm_board(self):
        self.place(name='<img src=x onerror=alert(1)>')
        board = requests.get(self.st.crm_srv.url).text
        self.assertNotIn('<img src=x', board)
        self.assertIn('&lt;img src=x', board)


class SettingsTests(unittest.TestCase):
    def test_from_env_requires_all_secrets(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                Settings.from_env()
            self.assertIn('CRM_URL', str(cm.exception))
        env = {k: 'v' for k in ('CRM_URL', 'CRM_TOKEN', 'PAY_URL', 'PAY_KEY', 'WEBHOOK_SECRET', 'MSG_URL', 'LEAD_TOKEN', 'ACCESS_SECRET')}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Settings.from_env()
            self.assertEqual((cfg.db_path, cfg.manager_chat), ('integration.db', '@demo_manager'))
        with mock.patch.dict(os.environ, {**env, 'MANAGER_CHAT': '@boss', 'ACCESS_BASE': 'https://x.example/a',
                                          'DB_PATH': 'x.db'}, clear=True):
            cfg = Settings.from_env()
            self.assertEqual((cfg.manager_chat, cfg.access_base, cfg.db_path), ('@boss', 'https://x.example/a', 'x.db'))


if __name__ == '__main__':
    unittest.main()
