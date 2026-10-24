import unittest

from integration.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        self.addCleanup(self.s.close)

    def new(self, lead='l1', **kw):
        return self.s.create_order(lead, 'fp', 'Анна', 'a@example.com', 'audit', 'Аудит', 1_200_000, **kw)

    def test_order_is_idempotent_by_lead_id(self):
        a, created_a = self.new()
        b, created_b = self.new()
        self.assertEqual((a['ref'], created_a, b['ref'], created_b), ('ORD-0001', True, 'ORD-0001', False))
        self.assertEqual(self.new('l2')[0]['ref'], 'ORD-0002')

    def test_steps_are_written_with_the_order_and_unique(self):
        order, _ = self.new(steps=[('crm_deal', 'CRM'), ('invoice', 'PAY')])
        self.new(steps=[('crm_deal', 'CRM')])                                # повторная заявка шагов не добавляет
        self.s.add_step(order['ref'], 'invoice', 'PAY')
        self.assertEqual([x['key'] for x in self.s.pending_steps()], ['crm_deal', 'invoice'])

    def test_status_change_is_compare_and_set(self):
        ref = self.new()[0]['ref']
        self.assertTrue(self.s.set_status(ref, 'AWAITING_PAYMENT', ('NEW',)))
        self.assertFalse(self.s.set_status(ref, 'AWAITING_PAYMENT', ('NEW',)))      # уже не NEW
        self.assertEqual(self.s.order(ref)['status'], 'AWAITING_PAYMENT')

    def test_event_is_recorded_once(self):
        self.assertTrue(self.s.record_event('evt_1', 'payment.succeeded', 'ORD-0001'))
        self.assertFalse(self.s.record_event('evt_1', 'payment.succeeded', 'ORD-0001'))
        self.s.set_event_outcome('evt_1', 'paid')
        self.assertEqual((self.s.event_outcome('evt_1'), self.s.event_outcome('nope')), ('paid', None))

    def test_step_becomes_dead_after_limit(self):
        ref = self.new(steps=[('crm_deal', 'CRM')])[0]['ref']
        step = self.s.pending_steps(ref)[0]
        self.assertEqual(self.s.fail_step(step['id'], 'err', 2), 'pending')
        self.assertEqual(self.s.fail_step(step['id'], 'err', 2), 'dead')
        self.assertEqual(self.s.pending_steps(ref), [])
        self.assertEqual(self.s.stats()['steps'], {'dead': 1})

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.s.tx():
                self.s.record_event('evt_1', 't', 'r')
                raise RuntimeError('сбой посреди операции')
        self.assertIsNone(self.s.event_outcome('evt_1'))
        self.assertTrue(self.s.record_event('evt_1', 't', 'r'))              # блокировка снята, база работает

    def test_unknown_columns_are_rejected(self):
        ref = self.new()[0]['ref']
        with self.assertRaises(AssertionError):
            self.s.update_order(ref, status='PAID')       # статус меняется только через set_status


if __name__ == '__main__':
    unittest.main()
