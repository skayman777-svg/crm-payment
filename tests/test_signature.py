import unittest

from integration.signature import SignatureError, sign, verify
from mocks import pay

SECRET, BODY = 'whsec-test', b'{"id":"evt_1","type":"payment.succeeded"}'


class SignatureTests(unittest.TestCase):
    def test_valid_signature_passes(self):
        self.assertEqual(verify(SECRET, BODY, sign(SECRET, BODY, 1000), now=1000), 1000)

    def test_provider_and_service_implementations_agree(self):
        # провайдер считает подпись своим кодом: формат должен совпасть байт в байт
        self.assertEqual(pay.sign(SECRET, BODY, 1234), sign(SECRET, BODY, 1234))

    def test_changed_body_is_rejected(self):
        with self.assertRaisesRegex(SignatureError, 'не совпала'):
            verify(SECRET, BODY + b' ', sign(SECRET, BODY, 1000), now=1000)

    def test_wrong_secret_is_rejected(self):
        with self.assertRaisesRegex(SignatureError, 'не совпала'):
            verify(SECRET, BODY, sign('other', BODY, 1000), now=1000)

    def test_missing_header_is_rejected(self):
        for header in (None, ''):
            with self.assertRaisesRegex(SignatureError, 'отсутствует'):
                verify(SECRET, BODY, header)

    def test_malformed_header_is_rejected(self):
        for header in ('garbage', 't=abc,v1=00', 't=1000', 'v1=00', 't=1000,v1'):
            with self.assertRaisesRegex(SignatureError, 'формате'):
                verify(SECRET, BODY, header, now=1000)

    def test_non_ascii_signature_does_not_crash(self):
        with self.assertRaises(SignatureError):
            verify(SECRET, BODY, 't=1000,v1=подпись', now=1000)

    def test_timestamp_window_is_enforced_in_both_directions(self):
        header = sign(SECRET, BODY, 1000)
        verify(SECRET, BODY, header, tolerance=300, now=1300)              # ровно на границе: допустимо
        for now in (1301, 699):
            with self.assertRaisesRegex(SignatureError, 'метка времени'):
                verify(SECRET, BODY, header, tolerance=300, now=now)

    def test_signature_binds_timestamp(self):
        # старую подпись нельзя «освежить», подставив новое время: MAC считается и от времени
        mac = sign(SECRET, BODY, 1000).split('v1=')[1]
        with self.assertRaisesRegex(SignatureError, 'не совпала'):
            verify(SECRET, BODY, f't=1100,v1={mac}', now=1100)


if __name__ == '__main__':
    unittest.main()
