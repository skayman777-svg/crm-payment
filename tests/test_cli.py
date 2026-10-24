import os
import unittest
from unittest import mock

from integration.__main__ import main
from tests.base import StandCase


class CliTests(StandCase):
    def env(self):
        c = self.st.cfg
        return {'CRM_URL': c.crm_url, 'CRM_TOKEN': c.crm_token, 'PAY_URL': c.pay_url, 'PAY_KEY': c.pay_key,
                'WEBHOOK_SECRET': c.webhook_secret, 'MSG_URL': c.msg_url, 'LEAD_TOKEN': c.lead_token,
                'ACCESS_SECRET': c.access_secret, 'DB_PATH': ':memory:'}

    def test_reconcile_command_exits_zero_when_all_is_well(self):
        with mock.patch.dict(os.environ, self.env()), self.assertLogs('svc', 'INFO') as logs:
            self.assertEqual(main(['reconcile']), 0)
        self.assertIn('сверка завершена', logs.output[-1])

    def test_serve_command_starts_the_web_app(self):
        with mock.patch.dict(os.environ, self.env()), mock.patch('flask.Flask.run') as run:
            self.assertEqual(main(['serve', '--port', '9191']), 0)
        run.assert_called_once_with(host='127.0.0.1', port=9191, threaded=True)

    def test_missing_settings_stop_the_start(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                main(['reconcile'])

    def test_unknown_command_is_refused(self):
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            main(['explode'])


if __name__ == '__main__':
    unittest.main()
