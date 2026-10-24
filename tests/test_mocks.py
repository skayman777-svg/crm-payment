"""Учебные сервисы тоже проверяем: если макет ведёт себя иначе, чем заявлено, тесты интеграции ничего не стоят."""
import unittest

from flask import Flask, jsonify

from mocks import crm, messenger, pay
from mocks._server import ServerThread

AUTH = {'Authorization': 'Bearer crm-demo-token'}


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.app = crm.create_app()
        self.c = self.app.test_client()

    def deal(self, ext='ORD-1'):
        return self.c.post('/api/deals', json={'external_id': ext, 'title': 'T', 'amount_kop': 100,
                                               'contact': {'name': 'A', 'email': 'a@example.com'}}, headers=AUTH)

    def test_token_is_required(self):
        self.assertEqual(self.c.post('/api/deals', json={}).status_code, 401)
        self.assertEqual(self.c.post('/api/deals', json={}, headers={'Authorization': 'Bearer x'}).status_code, 401)

    def test_deal_is_idempotent_by_external_id(self):
        a, b = self.deal(), self.deal()
        self.assertEqual((a.status_code, b.status_code, a.json['id'] == b.json['id']), (201, 200, True))
        self.assertEqual(len(self.app.crm_state.deals), 1)

    def test_external_id_is_required(self):
        self.assertEqual(self.c.post('/api/deals', json={'title': 'x'}, headers=AUTH).status_code, 422)

    def test_stage_transitions_are_validated(self):
        did = self.deal().json['id']
        patch = lambda stage: self.c.patch(f'/api/deals/{did}', json={'stage': stage}, headers=AUTH).status_code
        self.assertEqual(patch('PAID'), 409)                     # через ожидание оплаты нельзя перепрыгнуть
        self.assertEqual([patch('AWAITING_PAYMENT'), patch('PAYMENT_FAILED'), patch('PAID'), patch('ACCESS_GRANTED')],
                         [200, 200, 200, 200])
        self.assertEqual(patch('NEW'), 409)                      # назад из финальной стадии нельзя
        self.assertEqual(patch('ACCESS_GRANTED'), 200)           # та же стадия: без изменений

    def test_comment_uid_deduplicates(self):
        did = self.deal().json['id']
        post = lambda: self.c.post(f'/api/deals/{did}/comments', json={'uid': 'u1', 'text': 'раз'}, headers=AUTH)
        self.assertEqual([post().status_code, post().status_code], [201, 200])
        self.assertEqual(len(self.app.crm_state.deals[did]['timeline']), 1)

    def test_failure_simulation_returns_503_then_recovers(self):
        self.app.crm_state.fail = 2
        codes = [self.c.get('/api/deals/101', headers=AUTH).status_code for _ in range(3)]
        self.assertEqual(codes, [503, 503, 404])

    def test_pages_escape_user_data(self):
        self.c.post('/api/deals', json={'external_id': 'X', 'title': '<script>alert(1)</script>', 'amount_kop': 100,
                                        'contact': {'name': '<b>x</b>', 'email': 'a@example.com'}}, headers=AUTH)
        for url in ('/', '/deals/101'):
            html = self.c.get(url).get_data(as_text=True)
            self.assertNotIn('<script>alert', html)
            self.assertIn('&lt;script&gt;', html)
        self.assertEqual(self.c.get('/deals/999').status_code, 404)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.prov = pay.Provider(delays=(0, 0.01, 0.01))
        self.c = pay.create_app(self.prov).test_client()
        self.body = {'order_ref': 'ORD-1', 'amount_kop': 590000, 'currency': 'RUB', 'description': 'Курс'}

    def create(self, key='ORD-1', body=None, auth=True):
        headers = {'Authorization': 'Bearer pay-demo-key'} if auth else {}
        if key:
            headers['Idempotence-Key'] = key
        return self.c.post('/v1/invoices', json=body or self.body, headers=headers)

    def test_auth_and_idempotence_key_are_required(self):
        self.assertEqual(self.create(auth=False).status_code, 401)
        self.assertEqual(self.create(key=None).status_code, 400)

    def test_same_key_returns_same_invoice(self):
        a, b = self.create(), self.create()
        self.assertEqual((a.status_code, b.status_code, a.json['id'] == b.json['id']), (201, 200, True))
        self.assertEqual(len(self.prov.invoices), 1)

    def test_same_key_with_other_body_is_refused(self):
        self.create()
        self.assertEqual(self.create(body={**self.body, 'amount_kop': 1}).status_code, 422)

    def test_amount_is_validated(self):
        for amount in (0, -5, 10.5, '100', True):
            self.assertEqual(self.create(key=f'k{amount}', body={**self.body, 'amount_kop': amount}).status_code, 422, amount)

    def test_checkout_page_and_payment(self):
        inv = self.create().json
        page = self.c.get(f'/pay/{inv["id"]}').get_data(as_text=True)
        self.assertIn('5 900 ₽', page)
        self.assertIn('Оплатить', page)
        self.prov.webhook_url = 'http://127.0.0.1:9/none'                 # доставка не нужна для этой проверки
        self.prov.delays = (0,)
        self.assertEqual(self.c.post(f'/pay/{inv["id"]}', data={'outcome': 'success'}).status_code, 303)
        self.assertEqual(self.prov.invoices[inv['id']]['status'], 'paid')
        self.assertIn('Счёт оплачен', self.c.get(f'/pay/{inv["id"]}').get_data(as_text=True))

    def test_webhook_is_retried_until_2xx(self):
        got = []
        app = Flask('receiver')

        @app.post('/hook')
        def hook():
            from flask import request
            got.append(request.headers.get('X-Demo-Signature'))
            return jsonify(ok=True), (500 if len(got) < 3 else 200)

        srv = ServerThread(app).start()
        self.addCleanup(srv.stop)
        self.prov.webhook_url = f'{srv.url}/hook'
        self.prov.delays = (0, 0.01, 0.01, 0.01)
        inv = self.create().json
        _, codes = self.prov.complete(inv['id'], 'success')
        self.assertEqual(codes, [500, 500, 200])
        self.assertTrue(all(h.startswith('t=') and ',v1=' in h for h in got))

    def test_delivery_gives_up_after_all_attempts(self):
        self.prov.webhook_url = 'http://127.0.0.1:9/none'
        self.prov.delays = (0, 0.01)
        inv = self.create().json
        _, codes = self.prov.complete(inv['id'], 'success')
        self.assertEqual(codes, [0, 0])                 # соединения нет: оба раза


class MessengerTests(unittest.TestCase):
    def test_send_validates_and_stores(self):
        app = messenger.create_app()
        c = app.test_client()
        self.assertEqual(c.post('/send', json={'channel': 'email'}).status_code, 422)
        self.assertEqual(c.post('/send', json={'channel': 'email', 'to': 'a@example.com', 'text': 'Тема\n\nТекст'}).status_code, 201)
        self.assertEqual(len(c.get('/messages').json), 1)
        c.post('/_sim/fail', json={'count': 1})
        self.assertEqual(c.post('/send', json={'channel': 'email', 'to': 'a@example.com', 'text': 'x'}).status_code, 503)


if __name__ == '__main__':
    unittest.main()
