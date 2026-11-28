import io
import json
import tempfile
import sys
import types
import importlib
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from research.trader_review.bingx_execution import (
    BingXAPIError, BingXExecution, ReconciliationRequired,
)


class SubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = self.folder.name + '/journal.sqlite'
        self.execution = BingXExecution('private-key', 'private-secret', self.path,
                                        trading_enabled=True)
        self.mode_patch = patch.object(BingXExecution, 'hedge_mode', return_value=False)
        self.mode_patch.start()

    def tearDown(self):
        self.mode_patch.stop()
        self.folder.cleanup()

    def test_hedge_mode_uses_correct_position_for_opens_and_reductions(self):
        for side, reduction, expected in [('BUY', False, 'LONG'), ('SELL', False, 'SHORT'),
                                           ('SELL', True, 'LONG'), ('BUY', True, 'SHORT')]:
            with self.subTest(side=side, reduction=reduction):
                with patch.object(self.execution, 'hedge_mode', return_value=True), \
                        patch.object(self.execution, '_request', return_value={}) as request:
                    self.execution.submit(str((side,reduction)), 'HYPE-USDT', side,
                                          Decimal('1'), reduce_only=reduction)
                    post = request.call_args_list[0]
                    self.assertEqual(post.kwargs['positionSide'], expected)
                    self.assertNotIn('reduceOnly', post.kwargs)

    def test_mode_query_uses_v1_endpoint_and_parses_string_bool(self):
        self.mode_patch.stop()
        for value, expected in [(True, True), ('true', True), (False, False), ('false', False)]:
            with patch.object(self.execution, '_request', return_value={'dualSidePosition': value}) as request:
                self.assertEqual(self.execution.hedge_mode(), expected)
                request.assert_called_once_with('GET', 'positionSide/dual', version=1)
        self.mode_patch.start()

    def test_http_400_preserves_api_code_message_and_redacts_credentials(self):
        body = io.BytesIO(json.dumps({'code': 109400,
            'msg': 'bad positionSide private-key private-secret'}).encode())
        error = HTTPError('https://example.invalid', 400, 'Bad request', {}, body)
        with patch('research.trader_review.bingx_execution.urlopen', side_effect=error):
            with self.assertRaises(BingXAPIError) as caught:
                self.execution._request('GET', 'trade/order')
        self.assertEqual(caught.exception.code, 109400)
        self.assertIn('bad positionSide', str(caught.exception))
        self.assertNotIn('private-key', str(caught.exception))
        self.assertNotIn('private-secret', str(caught.exception))

    def test_rejected_post_is_not_searched_or_resubmitted_after_restart(self):
        with patch.object(self.execution, '_request', side_effect=BingXAPIError(109400, 'bad quantity')) as request:
            with self.assertRaises(BingXAPIError):
                self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1'))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args, ('POST', 'trade/order'))
        restarted = BingXExecution('private-key', 'private-secret', self.path, trading_enabled=True)
        with patch.object(restarted, '_request') as request:
            with self.assertRaisesRegex(ReconciliationRequired, 'bad quantity'):
                restarted.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1'))
            request.assert_not_called()

    def test_timeout_is_ambiguous_and_never_triggers_a_second_post(self):
        with patch.object(self.execution, '_request', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1'))
        with patch.object(self.execution, '_request', side_effect=BingXAPIError(109421, 'not found')) as request:
            with self.assertRaisesRegex(ReconciliationRequired, 'not found'):
                self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1'))
            self.assertEqual(request.call_args.args, ('GET', 'trade/order'))
            self.assertEqual(request.call_count, 1)

    def test_successful_submission_and_reconciliation_keep_same_client_id(self):
        filled = {'status': 'FILLED', 'executedQty': '1'}
        with patch.object(self.execution, '_request', side_effect=[{}, filled, filled]) as request:
            self.assertEqual(self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1')), filled)
            self.assertEqual(self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1')), filled)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['POST', 'GET', 'GET'])
            self.assertEqual(request.call_args_list[0].kwargs['clientOrderId'],
                             request.call_args_list[1].kwargs['clientOrderId'])

    def test_changed_order_cannot_overwrite_saved_request(self):
        with patch.object(self.execution, '_request', side_effect=BingXAPIError(109400, 'bad quantity')):
            with self.assertRaises(BingXAPIError):
                self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('1'))
        with patch.object(self.execution, '_request') as request:
            with self.assertRaises(ValueError):
                self.execution.submit('signal', 'HYPE-USDT', 'BUY', Decimal('2'))
            request.assert_not_called()

    def test_worker_stays_alive_and_retries_polling_when_copy_is_blocked(self):
        # Import without network/feed dependencies, then exercise the actual
        # main loop. The trading adapter and all polling are mocked.
        feed = types.ModuleType('research.trader_review.finmonster_feed')
        feed.Observer = unittest.mock.Mock()
        hyper = types.ModuleType('research.trader_review.hyperliquid_feed')
        hyper.FillObserver = unittest.mock.Mock()
        worker = importlib.import_module('research.trader_review.bingx_copy_worker')
        class EndTest(Exception):
            pass
        argv = ['worker', '--db', self.folder.name + '/hyperliquid-bingx-live.sqlite',
                '--address', '0x' + '1' * 40, '--enable-orders']
        with patch.object(sys, 'argv', argv), patch.dict('os.environ', {
                'BINGX_API_KEY': 'test', 'BINGX_API_SECRET': 'test', 'BINGX_ACCOUNT_UID': '123'}), \
                patch.object(worker, 'Observer', feed.Observer), \
                patch.object(worker, 'FillObserver', hyper.FillObserver), \
                patch.object(worker, 'BingXExecution'), patch.object(worker, 'HyperliquidBingXCopy'), \
                patch('research.trader_review.late_entry_probe.start_probe'), \
                patch('research.trader_review.copy_health.start_health'), \
                patch.object(worker, 'cycle', side_effect=[
                    TimeoutError('private-key'), ValueError('invalid response'),
                    RuntimeError('unconfirmed execution'), ReconciliationRequired('blocked')]) as cycle, \
                patch.object(worker.time, 'sleep', side_effect=[None, None, None, EndTest]), \
                patch('builtins.print') as output:
            with self.assertRaises(EndTest):
                worker.main()
            self.assertEqual(cycle.call_count, 4)
            self.assertEqual(output.call_count, 4)
            self.assertNotIn('private-key', str(output.call_args_list))
            self.assertEqual(json.loads(output.call_args.args[0])['copy_status'], 'blocked')

    def test_failed_source_cannot_starve_peer_reduction_or_advance_its_own_cursor(self):
        worker = importlib.import_module('research.trader_review.bingx_copy_worker')
        sources = []
        rows = {}
        for index in range(2):
            db = Mock()
            db.execute.return_value.fetchone.return_value = None
            source = types.SimpleNamespace(address=str(index))
            copier = types.SimpleNamespace(db=db)
            execution = types.SimpleNamespace(copy_executed_entries_only=False, open_blocker=None)
            sources.append((source, copier, execution))
            times = (1, 2) if index == 0 else (3, 4)
            rows[id(db)] = [(n, json.dumps({'updatedAt': t}), None) for n, t in enumerate(times, 1)]
        processed = []
        def dispatch(source, copier, execution, **kwargs):
            row_id = kwargs['prepared_rows'][0][0]
            if source.address == '0':
                raise ReconciliationRequired('uncertain-order')
            processed.append(row_id)
            self.assertEqual(execution.open_blocker, 'another_copy_operation_requires_reconciliation')
        with patch.object(worker, 'synchronize'), \
                patch.object(worker, 'pending_rows', side_effect=lambda db: rows[id(db)]), \
                patch.object(worker, 'cycle', side_effect=dispatch) as cycle, \
                patch('builtins.print'):
            worker.cycle_sources(sources, min_equity=Decimal('80'), allowed_symbols=None)
        self.assertEqual(processed, [1, 2])
        self.assertEqual(cycle.call_count, 3)  # Failed source's second event never runs.
        self.assertTrue(all(call.args[0].startswith('SELECT')
                            for call in sources[0][1].db.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
