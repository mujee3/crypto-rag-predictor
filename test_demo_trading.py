import copy
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone
from dataclasses import replace
from trade_manager import TradeConfig,budget,entry_plan,Journal,Manager,dec
from demo_broker import DemoBroker,DEMO_HOST
from unittest.mock import Mock

NOW=1790890000
FILTERS=dict(tick='0.0001',min_price='0.0001',max_price='1000',step='1',min_qty='1',max_qty='1000000',min_notional='1')
def report(side='LONG'):
    return dict(symbol='SEI',time=datetime.fromtimestamp(NOW,timezone.utc).isoformat(),signal=side,ai={'status':'observed'},snapshot={'candle_close_ms':123},hypothetical_plan={'reference_entry':'.1','stop_reference':'.095' if side=='LONG' else '.105','target_reference':'.115' if side=='LONG' else '.085'})
class Broker:
    def __init__(self):
        self.balance=dict(wallet='50',available='50',initial_margin='0',positions={})
        self.entries={};self.exits=[];self.enter_count=0;self.close_count=0;self.fail_entry=False;self.fail_protect=False;self.partial=False
    def account(self):return copy.deepcopy(self.balance)
    def orders(self):return []
    def algos(self):return copy.deepcopy(self.exits)
    def filters(self,s):return FILTERS
    def quote(self,s):return dict(bid='.09999',ask='.10001',timestamp_ms=NOW*1000)
    def prepare(self,*args):pass
    def enter(self,row):
        self.enter_count+=1
        qty=dec(row['quantity'])/(2 if self.partial else 1)
        self.entries[row['entry_id']]=dict(status='EXPIRED' if self.partial else 'FILLED',executedQty=str(qty),avgPrice='.1')
        self.balance['positions'][row['symbol']]={'positionAmt':str(qty if row['side']=='BUY' else -qty)}
        self.balance.update(available='48.5',initial_margin='1.5')
        if self.fail_entry:raise TimeoutError()
    def order(self,row):return self.entries[row['entry_id']]
    def protect(self,row,kind):
        if self.fail_protect:raise TimeoutError()
        self.exits.append(dict(clientAlgoId=row[kind+'_id'],symbol=row['symbol'],side='SELL' if row['side']=='BUY' else 'BUY',closePosition=True,orderType='STOP_MARKET' if kind=='sl' else 'TAKE_PROFIT_MARKET',triggerPrice=row['stop'] if kind=='sl' else row['target'],workingType='CONTRACT_PRICE',positionSide='BOTH'))
    def cancel_algo(self,identity):self.exits=[x for x in self.exits if x['clientAlgoId']!=identity]
    def close(self,row,quantity):self.close_count+=1;self.balance['positions'].pop(row['symbol'],None)

class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'journal.db';self.journal=Journal(self.path);self.addCleanup(self.journal.close)
        self.broker=Broker();self.config=TradeConfig();self.now=NOW
        self.manager=Manager(self.broker,self.journal,self.config,lambda:self.now)
    def test_budget_fifty_and_exhausted(self):
        b=budget(self.broker.account(),self.config)
        self.assertEqual(dec(b['trade_margin']),dec('1.5'));self.assertEqual(dec(b['free']),dec('45'))
        self.broker.balance.update(available='1',initial_margin='49')
        self.assertFalse(budget(self.broker.account(),self.config)['can_enter'])
    def test_pending_reservation_and_cap(self):
        self.assertFalse(budget(self.broker.account(),self.config,reserved='44')['can_enter'])
        self.assertFalse(budget(self.broker.account(),replace(self.config,max_total_margin_usdt=1))['can_enter'])
    def test_filters_never_upsize(self):
        with self.assertRaisesRegex(ValueError,'minimum notional'):
            entry_plan(report(),self.broker.account(),self.config,{**FILTERS,'min_notional':'5'},self.broker.quote(''),NOW)
    def test_long_short_sizing(self):
        for side in ('LONG','SHORT'):
            p=entry_plan(report(side),self.broker.account(),self.config,FILTERS,self.broker.quote(''),NOW)
            self.assertLessEqual(dec(p['quantity'])*max(dec(p['limit_price']),dec('.10001')),dec('1.5'))
    def test_stale_signal_and_quote(self):
        with self.assertRaisesRegex(ValueError,'expired'):entry_plan(report(),self.broker.account(),self.config,FILTERS,self.broker.quote(''),NOW+61)
        with self.assertRaisesRegex(ValueError,'quote expired'):entry_plan(report(),self.broker.account(),self.config,FILTERS,{**self.broker.quote(''),'timestamp_ms':0},NOW)
    def test_position_and_pending_order_block(self):
        self.broker.balance['positions']['SEIUSDT']={'positionAmt':'1'};self.manager.poll()
        self.assertFalse(self.manager.can_scan('SEIUSDT')[0])
        self.broker.balance['positions']={};self.broker.orders=lambda:[{'symbol':'SEIUSDT'}];self.manager.poll()
        self.assertFalse(self.manager.can_scan('SEIUSDT')[0])
    def test_entry_and_protection_no_duplicate(self):
        self.manager.submit(report());self.manager.submit(report())
        self.assertEqual(self.broker.enter_count,1);self.assertEqual(len(self.broker.exits),2)
        self.assertEqual(self.journal.active()[0]['state'],'OPEN')
    def test_ambiguous_entry_reconciled_without_resubmit(self):
        self.broker.fail_entry=True;self.manager.submit(report());self.manager.poll()
        self.assertEqual(self.broker.enter_count,1);self.assertEqual(self.journal.active()[0]['state'],'OPEN')
    def test_partial_fill_protected(self):
        self.broker.partial=True;self.manager.submit(report())
        self.assertEqual(self.journal.active()[0]['state'],'OPEN');self.assertEqual(len(self.broker.exits),2)
    def test_failed_protection_closes_once(self):
        self.broker.fail_protect=True;self.manager.submit(report());self.now+=10;self.manager.poll();self.manager.poll()
        self.assertEqual(self.broker.close_count,1);self.assertFalse(self.journal.active())
    def test_close_cleanup_cooldown_and_same_candle(self):
        self.manager.submit(report());self.broker.balance['positions']={};self.now+=10;self.manager.poll()
        self.assertFalse(self.broker.exits);self.assertFalse(self.manager.can_scan('SEIUSDT')[0])
        self.now+=301;self.manager.poll();self.assertTrue(self.manager.can_scan('SEIUSDT')[0])
        self.assertIn('already attempted',self.manager.submit(report())['reason'])
    def test_changed_position_requires_review(self):
        self.manager.submit(report());self.broker.balance['positions']['SEIUSDT']['positionAmt']='999';self.manager.poll()
        self.assertEqual(self.journal.active()[0]['state'],'MANUAL_REVIEW');self.assertFalse(self.manager.can_scan('BTCUSDT')[0]);self.assertEqual(self.broker.close_count,0)
    def test_unknown_entry_blocks_after_restart(self):
        self.manager.submit(report());self.broker.entries={}
        manager=Manager(self.broker,self.journal,self.config,lambda:self.now);manager.poll()
        self.assertFalse(manager.can_scan('BTCUSDT')[0]);self.assertEqual(self.broker.enter_count,1)
    def test_lock_and_live_rejection(self):
        with self.assertRaises(ValueError):Journal(self.path)
        with self.assertRaises(ValueError):TradeConfig.load({'mode':'live'})
        self.assertEqual(DEMO_HOST,'https://demo-fapi.binance.com')
    def test_wrong_protection_triggers_reduce_only_exit(self):
        self.manager.submit(report());self.broker.exits[0]['triggerPrice']='.0001';self.manager.poll()
        self.assertEqual(self.broker.close_count,1)
    def test_runner_skips_scan_when_margin_exhausted(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from demo_runner import run_demo
        import json
        config_path=Path(self.tmp.name)/'coins.json'
        config_path.write_text(json.dumps({'coins':['SEI'],'trading':{'mode':'demo'}}))
        self.broker.balance.update(available='0',initial_margin='50')
        self.broker.verify_modes=lambda:None
        args=SimpleNamespace(coins=str(config_path),ask_binance_demo_key=False,cache_dir=self.tmp.name,output=str(Path(self.tmp.name)/'signals.jsonl'),interval=900,json=False,history_days=90)
        separate=Journal(Path(self.tmp.name)/'runner.db')
        with patch('demo_runner.Journal',return_value=separate),patch('demo_runner.DemoBroker',return_value=self.broker),patch('demo_runner.Service') as service,patch('demo_runner.time.sleep',side_effect=KeyboardInterrupt),patch('builtins.print'):
            run_demo(['SEI-USDT-SWAP'],args)
        service.assert_not_called()
    def test_close_adapter_is_reduce_only_and_exit_is_close_position(self):
        broker=DemoBroker.__new__(DemoBroker);broker.call=Mock()
        row={'symbol':'SEIUSDT','side':'BUY','close_id':'x','stop':'.095','sl_id':'s'}
        broker.close(row,'10');self.assertEqual(broker.call.call_args.kwargs['reduceOnly'],'true')
        broker.protect(row,'sl');self.assertEqual(broker.call.call_args.kwargs['closePosition'],'true')
        self.assertNotIn('quantity',broker.call.call_args.kwargs)

if __name__=='__main__':unittest.main()
