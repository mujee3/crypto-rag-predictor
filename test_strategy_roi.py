import copy
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock,patch
from roi import price_for_roi,net_roi
from signal_logic import trend_gate,gpt_status
from paper_compare import PaperLedger
from trade_manager import TradeConfig,Journal,Manager,dec
from demo_broker import DemoBroker
from test_system import snapshot,args,NOW
from test_demo_trading import Broker,report

class ROITests(unittest.TestCase):
    def test_net_targets_both_sides_and_leverages(self):
        for side in ('LONG','SHORT'):
            for leverage in (1,5,10):
                for target in (25,-10):
                    for funding in (-.3,0,.2):
                        price=price_for_roi(side,100,1,100/leverage,target,.05,funding,.0008)
                        pnl,roi=net_roi(side,100,1,100/leverage,price,.05,funding,.0008)
                        self.assertAlmostEqual(float(roi),target,places=10)
    def test_ten_margin_two_fifty_profit_one_loss(self):
        p=price_for_roi('LONG',100,1,10,25,0,0,0)
        self.assertEqual(net_roi('LONG',100,1,10,p,0,0,0)[0],dec('2.5'))
        p=price_for_roi('SHORT',100,1,10,-10,0,0,0)
        self.assertEqual(net_roi('SHORT',100,1,10,p,0,0,0)[0],dec('-1'))
    def test_bad_accounting_rejected(self):
        for margin in (0,-1,'NaN'):
            with self.assertRaises(ValueError):net_roi('LONG',100,1,margin,101,0,0,.001)
    def setup_snapshot(self,side='LONG'):
        s=snapshot();s['technical'].update(vwap_24h=99 if side=='LONG' else 101,ema9_15m=99 if side=='LONG' else 101)
        s['entry_setup']={'open_15m':99 if side=='LONG' else 101,'close_15m':100}
        return s
    def test_trend_both_sides_without_ai(self):
        for side in ('LONG','SHORT'):
            s=self.setup_snapshot(side);out=trend_gate(s,args(),s['book'],NOW)
            self.assertEqual(out['signal'],side);self.assertEqual(out['status'],'READY')
            self.assertEqual(out['plan']['take_profit_pct'],25)
    def test_context_disagreement_not_veto(self):
        s=self.setup_snapshot();s['technical']['relative_volume']=.1;s['funding']['rate_per_interval']=.02;s['book']['band_fully_visible']=False
        out=trend_gate(s,args(),s['book'],NOW)
        self.assertEqual(out['signal'],'LONG');self.assertEqual(len(out['warnings']),3)
    def test_wait_blocked_and_skipped_distinct(self):
        s=self.setup_snapshot();s['entry_setup']['open_15m']=101
        self.assertEqual(trend_gate(s,args(),s['book'],NOW)['status'],'WAIT')
        s=self.setup_snapshot();self.assertEqual(trend_gate(s,args(),None,NOW)['status'],'BLOCKED')
        s['book']['spread_bps']=100;self.assertEqual(trend_gate(s,args(),s['book'],NOW)['status'],'SKIPPED')
        self.assertEqual(gpt_status({'status':'daily_limit'},'WAIT',[]),'BLOCKED')
    def test_actual_fill_margin_and_net_exit_close_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            j=Journal(Path(tmp)/'db');b=Broker();b.commission_rate=lambda s:'.0005';b.entry_commission=lambda r,o:dec('.0007');b.funding_total=lambda r,end:dec(0)
            config=replace(TradeConfig(),strategy='trend',exit_mode='margin_roi')
            clock=lambda:1790890000
            m=Manager(b,j,config,clock);r=report();r.update(strategy='trend',decision_status='READY',ai={'status':'daily_limit'})
            try:
                m.submit(r);row=j.active()[0]
                self.assertEqual(row['state'],'OPEN');self.assertEqual(dec(row['initial_margin']),dec(row['filled_quantity'])*dec('.1'))
                b.quote=lambda s:dict(bid='.13',ask='.13001',timestamp_ms=clock()*1000)
                m.poll();self.assertEqual(b.close_count,1);self.assertFalse(b.balance['positions'])
            finally:j.close()
    def test_accounting_failure_keeps_protection_blocks_new_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            j=Journal(Path(tmp)/'db');b=Broker();b.commission_rate=lambda s:'.0005';b.entry_commission=Mock(side_effect=ValueError('missing commissions'))
            m=Manager(b,j,replace(TradeConfig(),strategy='trend',exit_mode='margin_roi'),lambda:1790890000)
            r=report();r.update(strategy='trend',decision_status='READY')
            try:
                m.submit(r);self.assertEqual(len(b.exits),2);self.assertTrue(j.active()[0]['roi_problem']);self.assertFalse(m.can_scan('BTCUSDT')[0])
            finally:j.close()
    def test_entry_fee_must_match_all_fills_and_asset(self):
        b=DemoBroker.__new__(DemoBroker);b.call=Mock(return_value=[dict(id=1,orderId=9,qty='2',commissionAsset='USDT',commission='.1')])
        self.assertEqual(b.entry_commission({'symbol':'SEIUSDT'},{'orderId':9,'executedQty':'2'}),dec('.1'))
        with self.assertRaises(ValueError):b.entry_commission({'symbol':'SEIUSDT'},{'orderId':9,'executedQty':'3'})
        b.call.return_value[0]['commissionAsset']='BNB'
        with self.assertRaises(ValueError):b.entry_commission({'symbol':'SEIUSDT'},{'orderId':9,'executedQty':'2'})
    def test_paper_does_not_overwrite_open_trade_or_repeat_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[NOW];p=PaperLedger(Path(tmp)/'db',lambda:now[0]);a=args();a.leverage=1;a.take_profit_pct=25;a.stop_loss_pct=10
            s=self.setup_snapshot();decision=trend_gate(s,a,s['book'],NOW)
            r={'instrument':'SEI-USDT-SWAP','snapshot':s,'post_analysis_quote':s['book'],'comparison':{'trend':decision}}
            try:
                p.observe(r,a);p.observe(r,a);self.assertEqual(len(p.journal.active()),1)
                p.mark(r['instrument'],{'bid':130,'ask':130.01,'timestamp_ms':NOW*1000},[])
                self.assertFalse(p.journal.active());self.assertEqual(p.summary()['trend']['wins'],1)
                now[0]+=400;r['post_analysis_quote']['timestamp_ms']=now[0]*1000
                p.observe(r,a);self.assertEqual(len(p.journal.all()),1)
                r['snapshot']['candle_close_ms']+=900000;p.observe(r,a);self.assertEqual(len(p.journal.all()),2)
            finally:p.close()
    def test_paper_funding_is_applied_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[NOW];p=PaperLedger(Path(tmp)/'db',lambda:now[0]);a=args();a.leverage=1;a.take_profit_pct=25;a.stop_loss_pct=10
            s=self.setup_snapshot();r={'instrument':'SEI-USDT-SWAP','snapshot':s,'post_analysis_quote':s['book'],'comparison':{'trend':trend_gate(s,a,s['book'],NOW)}}
            try:
                p.observe(r,a);now[0]+=20;quote={'bid':100,'ask':100.01,'timestamp_ms':now[0]*1000};events=[dict(fundingTime=(NOW+10)*1000,realizedRate='.001')]
                p.mark(r['instrument'],quote,events);funding=p.journal.active()[0]['funding'];p.mark(r['instrument'],quote,events)
                self.assertEqual(p.journal.active()[0]['funding'],funding);self.assertLess(funding,0)
            finally:p.close()

if __name__=='__main__':unittest.main()
