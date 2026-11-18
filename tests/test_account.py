import os
import time
import types
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication
from ai_boardroom.account_ui import fetch_account, AccountPanel


def response(data, status=200):
    return types.SimpleNamespace(status_code=status, json=lambda: {'data': data})


class AccountTests(unittest.TestCase):
    def test_real_balance_is_not_confused_with_api_key_spending_cap(self):
        with patch('requests.get', side_effect=[response({
            'free_model_daily_requests': {'used': 12, 'limit': 50, 'remaining': 38},
            'limit_remaining': 2, 'label': 'private'}),
            response({'total_credits': 10, 'total_usage': .75})]) as get:
            result = fetch_account('generation-secret', 'management-secret')
        self.assertEqual(result['quota']['remaining'], 38)
        self.assertEqual(result['credit_remaining'], '9.25')
        self.assertEqual(result['key_remaining'], 2)
        self.assertEqual(get.call_args_list[0].kwargs['headers']['Authorization'], 'Bearer generation-secret')
        self.assertEqual(get.call_args_list[1].kwargs['headers']['Authorization'], 'Bearer management-secret')
        self.assertNotIn('secret', str(result))
        self.assertNotIn('private', str(result))

    def test_no_credit_permission_does_not_display_zero_balance(self):
        with patch('requests.get', side_effect=[response({'free_model_daily_requests': {
            'used': 0, 'limit': 50, 'remaining': 50}}), response({}, 403)]):
            result = fetch_account('dummy')
        self.assertEqual(result['quota']['remaining'], 50)
        self.assertNotIn('credit_remaining', result)
        self.assertIn('yönetim anahtarı', result['credits_error'])

    def test_quota_failure_does_not_hide_valid_balance_or_leak_raw_error(self):
        with patch('requests.get', side_effect=[RuntimeError('Bearer secret'), response({'total_credits': 0, 'total_usage': 0})]):
            result = fetch_account('dummy')
        self.assertEqual(result['credit_remaining'], '0')
        self.assertNotIn('secret', str(result))
        self.assertNotIn('quota', result)

    def test_missing_keys_do_not_send_requests(self):
        with patch('requests.get') as get:
            result = fetch_account('')
        get.assert_not_called()
        self.assertNotIn('credit_remaining', result)

    def test_missing_or_invalid_values_are_unknown(self):
        with patch('requests.get', side_effect=[response({'free_model_daily_requests': {'used': False}}),
                                               response({'total_credits': float('nan'), 'total_usage': 1})]):
            result = fetch_account('dummy')
        self.assertNotIn('quota', result)
        self.assertNotIn('credit_remaining', result)

    def test_widget_refreshes_in_background_and_switches_key_source(self):
        app = QApplication.instance() or QApplication([])
        seen = []
        def loader(key, management):
            seen.append(key)
            time.sleep(.03)
            return {'quota': {'used': 2, 'limit': 50, 'remaining': 48},
                    'credit_remaining': '9.25', 'checked_at': 'TEST'}
        panel = AccountPanel(lambda source: source, loader=loader)
        try:
            panel.refresh()
            self.assertFalse(panel.refresh_button.isEnabled())
            for _ in range(200):
                app.processEvents()
                if panel.refresh_button.isEnabled():
                    break
                time.sleep(.005)
            self.assertIn('48 / 50', panel.quota_label.text())
            self.assertIn('$9.2500', panel.credit_label.text())
            panel.source.setCurrentIndex(1)
            panel.worker.wait(1000)
            app.processEvents()
            self.assertEqual(seen, ['free', 'paid'])
        finally:
            if panel.worker:
                panel.worker.wait(1000)
            panel.timer.stop()
            panel.close()
