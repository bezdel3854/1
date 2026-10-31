import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from research.trader_review.finmonster_feed import Feed, Observer, canonical, TRADER
from research.trader_review.finmonster_worker import retire_previous
from research.trader_review.finmonster_paper import Paper
from tests.test_finmonster_feed import FakeFeed, order
from tests.test_finmonster_paper import FakeMarket

NEW_ID = '1546598203650777088_1632593640919543874'


class TraderSwitchTests(unittest.TestCase):
    def test_selected_identity_and_new_baseline_survive_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = root+'/okey.sqlite'
            observer = Observer(path, NEW_ID, 'OkeyYT')
            feed = FakeFeed(); feed.profile = {'traderId': NEW_ID, 'name': 'OkeyYT'}
            observer.poll(feed, 1000)
            self.assertEqual(Paper(observer.db).step(FakeMarket(),1000)['positions'],0)
            observer.db.close()
            with self.assertRaisesRegex(ValueError, 'separate database'):
                Observer(path)
            observer = Observer(path, NEW_ID, 'OkeyYT')
            feed.pages[None]['orders'].insert(0, order('new',1100))
            self.assertEqual(observer.poll(feed,1200)['new_order_versions'],1)
            feed.profile['name'] = 'FinMonster'
            with self.assertRaisesRegex(ValueError,'identity mismatch'):
                observer.poll(feed,1300)
            observer.db.close()

    def test_feed_sends_only_the_selected_id(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'data': {'traderId':NEW_ID,'name':'OkeyYT'}})
        with patch('research.trader_review.finmonster_feed.urllib.request.urlopen',return_value=response) as network:
            feed=Feed(trader_id=NEW_ID,trader_name='OkeyYT')
            feed.get('traderProfile')
            self.assertIn('traderId='+NEW_ID,network.call_args.args[0].full_url)
            feed.get('traderProfile')
            self.assertEqual(network.call_count,1)

    def test_retirement_closes_at_executable_price_preserves_loss_and_budget(self):
        with tempfile.TemporaryDirectory() as root:
            old_path=root+'/old.sqlite'; old=Observer(old_path); paper=Paper(old.db)
            market=FakeMarket()
            event=dict(orderId='a',positionId='p',symbol='AGTUSDT',action='OPEN',side='BUY',
                       quantity=100,price=100,status='Filled',updatedAt=999990)
            old.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',('a','a',999990,1000000,'observed',canonical(event)))
            old.db.commit();paper.step(market,1000000)
            month=__import__('time').strftime('%Y-%m',__import__('time').gmtime())
            old.db.execute("INSERT INTO meta VALUES('feed_request_usage',?)",(canonical({'month':month,'requests':2975}),));old.db.commit();old.db.close()
            new=Observer(root+'/new.sqlite',NEW_ID,'OkeyYT')
            market.bid=99;market.ask=99.02
            with patch('research.trader_review.finmonster_paper.time.time_ns',return_value=1001000*1000000):
                market.stamp=1001000
                result=retire_previous(old_path,new.db,market)
                repeat=retire_previous(old_path,new.db,market)
            self.assertEqual(result,repeat);self.assertLess(result['cash'],1000)
            self.assertEqual(result['positions'],0)
            old=Observer(old_path)
            state=Paper(old.db).state();self.assertTrue(state['retired'])
            self.assertFalse(state['broker']['positions'])
            trade=json.loads(old.db.execute("SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
            self.assertEqual(trade['reason'],'trader_replaced');self.assertAlmostEqual(trade['exit'],99*.9998)
            self.assertEqual(old.db.execute("SELECT count(*) FROM paper_journal WHERE kind='trade'").fetchone()[0],1)
            with self.assertRaisesRegex(ValueError,'cannot resume'):Paper(old.db).step(market,1001000)
            self.assertEqual(json.loads(new.db.execute("SELECT value FROM meta WHERE key='feed_request_usage'").fetchone()[0])['requests'],2975)
            self.assertEqual(Paper(new.db).step(market,1001000)['cash'],1000)
            old.db.close();new.db.close()

    def test_unsupported_source_contract_is_skipped_without_blocking_cycle(self):
        with tempfile.TemporaryDirectory() as root:
            observer=Observer(root+'/new.sqlite',NEW_ID,'OkeyYT');market=FakeMarket()
            event=dict(orderId='unsupported',positionId='p',symbol='GOLDUSDT',action='OPEN',
                       side='BUY',quantity=1,price=100,status='Filled',updatedAt=999990)
            observer.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                ('unsupported','a',999990,1000000,'observed',canonical(event)));observer.db.commit()
            with patch.object(market,'rules',side_effect=KeyError('GOLD-USDT')):
                result=Paper(observer.db).step(market,1000000)
            self.assertEqual(result['positions'],0)
            decision=json.loads(observer.db.execute('SELECT result FROM paper_orders').fetchone()[0])
            self.assertEqual(decision['reason'],'unsupported_contract')
            self.assertEqual(Paper(observer.db).step(market,1000000)['source_decisions'],0)
            observer.db.close()
