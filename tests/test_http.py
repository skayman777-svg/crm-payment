import unittest

from flask import Flask, jsonify

from integration.http import Api, ApiError, Unavailable
from mocks._server import ServerThread


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.calls, self.sleeps = [], 0, []
        app = Flask('t')

        @app.get('/x')
        def x():
            code = self.plan[min(self.calls, len(self.plan) - 1)]
            self.calls += 1
            return jsonify(code=code), code

        @app.get('/slow')
        def slow():
            import time
            time.sleep(0.3)
            return jsonify(ok=True)

        self.srv = ServerThread(app).start()
        self.addCleanup(self.srv.stop)

    def api(self, **kw):
        kw.setdefault('attempts', 3)
        return Api('Тест', self.srv.url, backoff=0.1, jitter=False, sleep=self.sleeps.append, **kw)

    def test_retries_server_errors_then_succeeds(self):
        self.plan = [503, 502, 200]
        self.assertEqual(self.api().request('GET', '/x').status_code, 200)
        self.assertEqual((self.calls, self.sleeps), (3, [0.1, 0.2]))        # пауза растёт вдвое

    def test_429_is_retried(self):
        self.plan = [429, 200]
        self.assertEqual(self.api().request('GET', '/x').status_code, 200)

    def test_gives_up_after_all_attempts(self):
        self.plan = [503]
        with self.assertRaisesRegex(Unavailable, 'Тест недоступна.*попыток: 3'):
            self.api().request('GET', '/x')
        self.assertEqual(self.calls, 3)

    def test_client_errors_are_not_retried(self):
        self.plan = [422]
        with self.assertRaises(ApiError) as cm:
            self.api().request('GET', '/x')
        self.assertEqual((cm.exception.status, self.calls, self.sleeps), (422, 1, []))

    def test_connection_refused_is_unavailable(self):
        api = Api('Тест', 'http://127.0.0.1:9', attempts=2, backoff=0.01, jitter=False, sleep=self.sleeps.append)
        with self.assertRaisesRegex(Unavailable, 'нет соединения'):
            api.request('GET', '/x')
        self.assertEqual(len(self.sleeps), 1)

    def test_timeout_is_unavailable(self):
        with self.assertRaises(Unavailable):
            self.api(attempts=1, timeout=0.05).request('GET', '/slow')

    def test_jitter_stays_within_bounds(self):
        self.plan = [503]
        api = Api('Тест', self.srv.url, attempts=4, backoff=1.0, jitter=True, sleep=self.sleeps.append)
        with self.assertRaises(Unavailable):
            api.request('GET', '/x')
        for pause, base in zip(self.sleeps, (1.0, 2.0, 4.0)):
            self.assertTrue(0.75 * base <= pause <= 1.25 * base, (pause, base))


if __name__ == '__main__':
    unittest.main()
