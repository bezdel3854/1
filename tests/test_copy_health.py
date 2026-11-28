import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from research.trader_review.copy_health import snapshot


class HealthTests(unittest.TestCase):
    def test_readonly_snapshot_reports_stale_polls_and_pending_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'copy.sqlite'
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE meta(key TEXT,value TEXT);
                    INSERT INTO meta VALUES('hl_address','public-address');
                    CREATE TABLE polls(observed_ms INTEGER);
                    INSERT INTO polls VALUES(1000);
                    CREATE TABLE copied_positions(epoch TEXT);
                    CREATE TABLE copied_signals(id TEXT);
                    CREATE TABLE copy_skips(id TEXT);
                    CREATE TABLE copy_plans(id TEXT);
                    INSERT INTO copy_plans VALUES('pending-close'),('completed'),('skipped');
                    INSERT INTO copied_signals VALUES('completed');
                    INSERT INTO copy_skips VALUES('skipped');
                    CREATE TABLE copy_entry_children(adopted INTEGER);
                    INSERT INTO copy_entry_children VALUES(0);
                    CREATE TABLE copy_exit_dispatch(result TEXT);
                    INSERT INTO copy_exit_dispatch VALUES(NULL);
                """)
            before = path.read_bytes()
            execution = Mock()
            execution.equity.return_value = Decimal('99.9830')
            execution._request.return_value = [{'positionAmt': '0'}, {'positionAmt': '-.1'}]
            execution._orders.return_value = []
            result = snapshot(execution, [path], now_ms=90000)
            self.assertEqual(result['actual_exchange_equity'], '99.9830')
            self.assertEqual(result['exchange_positions'], 1)
            self.assertEqual(result['sources'][0]['poll_age_ms'], 89000)
            self.assertEqual(result['sources'][0]['pending_entries'], 1)
            self.assertEqual(result['sources'][0]['pending_exits'], 1)
            self.assertEqual(result['sources'][0]['pending_signals'], 1)
            execution._request.assert_called_once_with('GET', 'user/positions')
            self.assertEqual(path.read_bytes(), before)
            execution.verify_account_identity.side_effect = RuntimeError('secret-key')
            failed = snapshot(execution, [path], now_ms=90000)
            self.assertNotIn('actual_exchange_equity', failed)
            self.assertNotIn('account_uid_verified', failed)
            self.assertNotIn('secret-key', json.dumps(failed))
            self.assertEqual(failed['exchange_check_error'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
