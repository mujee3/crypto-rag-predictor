import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs
from demo_broker import DemoBroker,LiveBroker,DEMO_HOST,LIVE_HOST
from live_check import allocation_check,check_live
from trade_manager import TradeConfig,Journal,Manager,dec
from test_demo_trading import Broker,report,NOW
from run import main

LIVE=dict(mode='live',max_total_margin_usdt=2,cash_reserve_usdt=.25,leverage=1,max_wallet_balance_usdt=2,max_entry_attempts=1)

class LiveTests(unittest.TestCase):
    def test_separate_hosts_and_credentials(self):
        with patch.dict(os.environ,{'BINANCE_API_KEY':'live-key','BINANCE_API_SECRET':'live-secret','BINANCE_DEMO_API_KEY':'demo-key','BINANCE_DEMO_API_SECRET':'demo-secret'},clear=True),patch.object(DemoBroker,'sync_time'):
            live=LiveBroker();demo=DemoBroker()
        self.assertEqual((live.host,live.key),(LIVE_HOST,'live-key'))
        self.assertEqual((demo.host,demo.key),(DEMO_HOST,'demo-key'))
    def test_demo_credentials_cannot_fall_back_to_live(self):
        with patch.dict(os.environ,{'BINANCE_DEMO_API_KEY':'demo-key','BINANCE_DEMO_API_SECRET':'demo-secret'},clear=True):
            with self.assertRaisesRegex(ValueError,'BINANCE_API_KEY'):LiveBroker()
    def test_read_only_guard_blocks_mutations_before_transport(self):
        broker=LiveBroker.__new__(LiveBroker);broker.read_only=True;broker.opener=Mock()
        for method in ('POST','DELETE','PUT'):
            with self.assertRaisesRegex(ValueError,'Read-only'):broker.call(method,'/fapi/v1/order')
        broker.opener.open.assert_not_called()
    def test_signed_transport_exact_host_and_redacted_errors(self):
        with patch.dict(os.environ,{'BINANCE_API_KEY':'fake-key','BINANCE_API_SECRET':'fake-secret'},clear=True),patch.object(DemoBroker,'sync_time'):
            broker=LiveBroker()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=b'{"ok":true}'
        broker.opener=Mock();broker.opener.open.return_value=response
        broker.call('GET','/fapi/v2/account')
        req=broker.opener.open.call_args.args[0]
        self.assertEqual(urlsplit(req.full_url).netloc,'fapi.binance.com')
        self.assertIn('signature',parse_qs(urlsplit(req.full_url).query))
        self.assertNotIn('fake-secret',req.full_url)
        broker.opener.open.side_effect=RuntimeError('fake-secret')
        with self.assertRaisesRegex(Exception,'unknown outcome') as caught:broker.call('GET','/fapi/v2/account')
        self.assertNotIn('fake-secret',str(caught.exception))
    def test_two_usdt_keeps_three_percent_and_refuses_minimum(self):
        broker=Broker();broker.balance.update(wallet='2',available='2')
        result=allocation_check(broker,'SEIUSDT',broker.account(),TradeConfig.load(LIVE))
        self.assertEqual(dec(result['allocation_usdt']),dec('.06'))
        self.assertFalse(result['size_eligible']);self.assertEqual(broker.enter_count,0)
    def test_live_config_requires_explicit_limits(self):
        for field in ('max_total_margin_usdt','cash_reserve_usdt','leverage','max_wallet_balance_usdt','max_entry_attempts'):
            raw=dict(LIVE);raw.pop(field)
            with self.assertRaises(ValueError):TradeConfig.load(raw)
        for field in ('max_wallet_balance_usdt','max_entry_attempts'):
            with self.assertRaises(ValueError):TradeConfig.load({**LIVE,field:None})
    def test_read_only_check_calls_no_mutators_or_gpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'coins.json';file.write_text(json.dumps({'coins':['SEI'],'trading':LIVE}))
            broker=Broker();broker.verify_modes=Mock();broker.prepare=Mock();broker.enter=Mock();broker.balance.update(wallet='2',available='2')
            with patch('live_check.LiveBroker',return_value=broker) as constructor,patch('run.Service') as service,contextlib.redirect_stdout(io.StringIO()) as output:
                main(['--coins',str(file),'--check-live'])
            constructor.assert_called_once_with(read_only=True);broker.prepare.assert_not_called();broker.enter.assert_not_called();service.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())['orders_placed'])
    def test_conflicting_flags_reject_before_connection(self):
        for flags in (['--live-trading','--demo-trading'],['--check-live','--live-trading'],['--ask-binance-key'],['--live-trading','--coins','unused','--ask-binance-demo-key']):
            with patch('demo_broker.LiveBroker') as broker,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):main(flags)
                broker.assert_not_called()
    def test_demo_json_cannot_enable_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'coins.json';file.write_text(json.dumps({'coins':['SEI'],'trading':{'mode':'demo'}}))
            with patch('demo_broker.LiveBroker') as broker,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):main(['--coins',str(file),'--live-trading'])
                broker.assert_not_called()
    def test_journal_binds_environment_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp)/'test.db')
            try:
                journal.bind('live','one');journal.bind('live','one')
                with self.assertRaises(ValueError):journal.bind('demo','one')
                with self.assertRaises(ValueError):journal.bind('live','two')
                self.assertNotEqual(journal.db.execute('SELECT value FROM identity').fetchone()[0],'one')
            finally:journal.close()
    def test_wallet_ceiling_blocks_entries_but_monitoring_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Journal(Path(tmp)/'test.db');broker=Broker()
            try:
                manager=Manager(broker,journal,TradeConfig.load(LIVE),lambda:NOW);manager.poll()
                self.assertTrue(manager.healthy);self.assertIn('ceiling',manager.can_scan('SEIUSDT')[1])
                self.assertEqual(manager.submit(report())['status'],'skipped');self.assertEqual(broker.enter_count,0)
            finally:journal.close()
    def test_entry_attempt_limit_survives_restart_and_keeps_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.db';journal=Journal(path);broker=Broker()
            config=TradeConfig.load({**LIVE,'max_wallet_balance_usdt':50})
            manager=Manager(broker,journal,config,lambda:NOW);manager.submit(report())
            self.assertEqual(broker.enter_count,1);self.assertEqual(len(broker.exits),2)
            journal.close();journal=Journal(path)
            try:
                manager=Manager(broker,journal,config,lambda:NOW);manager.poll()
                self.assertTrue(manager.healthy);self.assertIn('Entry-attempt',manager.can_scan('ETHUSDT')[1])
                self.assertEqual(journal.active()[0]['state'],'OPEN')
            finally:journal.close()
    def test_live_prepare_rejects_auto_topup(self):
        broker=LiveBroker.__new__(LiveBroker);broker.mode='live'
        broker.call=Mock(side_effect=[{}, {'leverage':1},[dict(symbol='SEIUSDT',positionSide='BOTH',marginType='isolated',isAutoAddMargin='true')]])
        with self.assertRaisesRegex(ValueError,'auto-add'):broker.prepare('SEIUSDT',1)

if __name__=='__main__':unittest.main()
