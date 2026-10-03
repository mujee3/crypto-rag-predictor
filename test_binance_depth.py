import unittest
import time
from unittest.mock import patch
import binance_depth as b
N=1800000000000

def book(wall=300, stamp=N, update=1):
    return {'T':stamp,'lastUpdateId':update,
      'bids':[['99.99','1'],['99.98','1'],['99.97',str(wall)],['99.96','1'],['99.6','1']],
      'asks':[['100.01','1'],['100.02','1'],['100.03','1'],['100.04','1'],['100.4','1']]}

class Tests(unittest.TestCase):
    def test_walls_gap_and_notional(self):
        x=b.normalize(book(),N)
        r=b.summarize([x,x])
        self.assertEqual(r['walls'][0]['price'],99.97)
        self.assertAlmostEqual(r['walls'][0]['notional_usdt'],99.97*300)
        self.assertTrue(r['walls'][0]['persistent_in_all_samples'])
        self.assertEqual(r['distinct_update_ids'],1)
        self.assertTrue(r['liquidity_gaps'])

    def test_reduced_wall_not_called_cancelled(self):
        a=b.normalize(book(),N);z=b.normalize(book(1, N+2000, 2),N+2000)
        r=b.summarize([a,z])
        self.assertFalse(r['walls'])
        self.assertEqual(r['wall_changes'][0]['observation'],'reduced_or_absent')
        self.assertGreater(r['wall_changes'][0]['current_usdt'],0)

    def test_previous_scan_wall(self):
        a=b.normalize(book(),N);z=b.normalize(book(400,N+60000,2),N+60000)
        self.assertEqual(b.summarize([z,z],previous=a)['wall_changes'][0]['observation'],'grew')

    def test_stale_future_crossed_invalid(self):
        for raw in [book(stamp=N-31000),book(stamp=N+1)]:
            with self.assertRaises(ValueError): b.normalize(raw,N)
        r=book();r['bids'][0][0]='101'
        with self.assertRaises(ValueError): b.normalize(r,N)
        r=book();r['asks'][0][1]='nan'
        with self.assertRaises(ValueError): b.normalize(r,N)

    def test_trade_side_dedup(self):
        trades=[{'a':1,'T':N,'m':True,'p':'100','q':'200'},
                {'a':2,'T':N,'m':False,'p':'100','q':'1'}]
        r=b.trades_summary(trades+trades,N)
        self.assertEqual(r['count'],2)
        self.assertEqual(r['sell_usdt'],20000)
        self.assertEqual(r['buy_usdt'],100)
        self.assertLess(r['imbalance'],0)

    def test_confirmation_freshness_and_both_sides(self):
        r={'status':'ok','book':{'timestamp_ms':N,'imbalance':-.8},'trades':{'latest_timestamp_ms':N,'imbalance':-.9}}
        self.assertTrue(b.confirmation_blocks(r,'LONG',N))
        self.assertFalse(b.confirmation_blocks(r,'SHORT',N))
        self.assertFalse(b.confirmation_blocks(r,'LONG',N+31000))
        r['trades']['imbalance']=.7
        self.assertFalse(b.confirmation_blocks(r,'LONG',N))

    def test_network_failures_are_reported(self):
        class Bad:
            def validate_symbol(self,symbol): raise RuntimeError('HTTP 451')
        r,prev=b.collect(Bad(),'SEIUSDT')
        self.assertEqual(r['status'],'unavailable')
        self.assertIsNone(prev)

    def test_complete_collection(self):
        class Fake:
            def validate_symbol(self,symbol):pass
            def get(self,path,**params):
                if path=='depth': return book()
                return [{'a':1,'T':N,'m':False,'p':'100','q':'2'}]
        with patch('binance_depth.time.time',return_value=N/1000),patch('binance_depth.time.sleep'):
            r,prev=b.collect(Fake(),'SEIUSDT')
        self.assertEqual(r['status'],'ok')
        self.assertEqual(r['book']['sample_count'],3)
        self.assertTrue(prev)
