import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from coin_discovery import canonical,timestamp,published,tickers,candidates,Cycle,discover,attach,universe

NOW=1790890000.
def post(age=60,text='$BTC LONG entry 1 TP 2',source='x'):
    pid=str(int((NOW-age)*1000-1288834974657)<<22)
    return dict(url='https://x.com/user/status/'+pid,excerpt=text,published_ts=NOW-age,time_check='fixture')
def args():return SimpleNamespace(discovery_hours=2,discovery_limit=10,discovery_daily_cap=24,discovery_source='web',interval=900)
class DiscoveryTests(unittest.TestCase):
    def test_only_individual_allowlisted_urls(self):
        for url in ('http://x.com/a/status/123','https://x.com.evil/a/status/123','https://x.com/user',
                    'https://user:pass@x.com/a/status/123','https://binance.com/en/square','https://x.com:444/a/status/123'):
            with self.assertRaises(ValueError):canonical(url)
        self.assertEqual(canonical('https://www.binance.com/en/square/post/123?x=1')[1],'square')
    def test_publication_dates_not_search_or_modified_dates(self):
        with patch('coin_discovery.fetch',return_value='<meta property="article:modified_time" content="2026-10-02T00:00:00Z">'):
            with self.assertRaises(ValueError):published('','square','1')
        with patch('coin_discovery.fetch',return_value='<meta property="article:published_time" content="2026-10-02T00:00:00Z">'):
            self.assertEqual(published('','square','1')[0],timestamp('2026-10-02T00:00:00Z'))
        with self.assertRaises(ValueError):timestamp('2026-10-02')
    def test_x_id_date(self):
        url,source,pid=canonical(post()['url'])
        self.assertAlmostEqual(published(url,source,pid)[0],NOW-60)
    def test_explicit_symbols_and_signal_words(self):
        self.assertEqual(tickers('$BTC long entry, ETH/USDT SHORT target'),{'BTC','ETH'})
        self.assertEqual(tickers('Bitcoin will rise'),set())
        self.assertEqual(tickers('$BTC news'),set())
    def test_age_dedup_and_supported_filter(self):
        items,rejected=candidates([post(),post(30),post(8000),post(-5),post(10,'$SCAM long entry')],{'BTC':'BTC-USDT-SWAP'},NOW,2,10)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['published_ts'],NOW-30);self.assertEqual(rejected,2)
        self.assertNotIn('excerpt',items[0]);self.assertNotIn('direction',items[0])
    def test_contract_intersection(self):
        c=Mock();c.api.get.return_value=[dict(instId='BTC-USDT-SWAP',state='live',settleCcy='USDT',ctType='linear',ctValCcy='BTC',ctMult='1'),dict(instId='ETH-USDT-SWAP',state='live',settleCcy='USDT',ctType='linear',ctValCcy='ETH')]
        c.binance.get.return_value={'symbols':[dict(baseAsset='BTC',symbol='BTCUSDT',status='TRADING',contractType='PERPETUAL',quoteAsset='USDT',marginAsset='USDT')]}
        self.assertEqual(universe(c),{'BTC':'BTC-USDT-SWAP'})
    def test_cycle_drains_waits_and_never_reuses_old_list(self):
        clock=[NOW];cycle=Cycle(args(),lambda:clock[0]);self.assertTrue(cycle.ready())
        cycle.accept({'status':'observed','candidates':[post(),post(8000)]})
        self.assertEqual(cycle.next()['published_ts'],NOW-60);self.assertFalse(cycle.ready())
        self.assertIsNone(cycle.next());self.assertFalse(cycle.ready());clock[0]+=900;self.assertTrue(cycle.ready())
        cycle.accept({'status':'BLOCKED','candidates':[post()]});self.assertIsNone(cycle.next())
    def test_expiry_after_scan_blocks_both_strategies(self):
        report=dict(signal='LONG',decision_status='READY',hypothetical_plan={},reasons=[],comparison={'trend':{'signal':'LONG','status':'READY'}})
        with patch('coin_discovery.time.time',return_value=NOW):attach(report,post(8000),args())
        self.assertEqual(report['signal'],'WAIT');self.assertEqual(report['comparison']['trend']['status'],'SKIPPED')
    def test_limits_and_errors_no_fallback(self):
        svc=Mock();svc.reserve.return_value='daily_limit'
        result=discover(svc,Mock(),args());self.assertEqual(result['reason'],'daily_limit');self.assertEqual(result['candidates'],[])
        svc.reserve.return_value=None
        with patch('coin_discovery.universe',side_effect=RuntimeError('secret should not print')):
            result=discover(svc,Mock(),args());self.assertNotIn('secret',json.dumps(result))
    def test_web_requires_retrieved_url_and_completed_search(self):
        svc=Mock();svc.reserve.return_value=None;svc.reasoning.return_value={};svc.model='fixture'
        row=post();other=post(30)
        response=Mock();response.output_text=json.dumps({'posts':[{'url':p['url'],'excerpt':p['excerpt']} for p in (row,other)]})
        response.model_dump.return_value={'output':[{'type':'web_search_call','status':'completed','action':{'sources':[{'url':row['url']}]}}]}
        svc.api.return_value.responses.create.return_value=response
        with patch('coin_discovery.time.time',return_value=NOW),patch('coin_discovery.universe',return_value={'BTC':'BTC-USDT-SWAP'}):
            result=discover(svc,Mock(),args())
            self.assertEqual(len(result['candidates']),1);self.assertEqual(result['unverified_posts_skipped'],1)
            response.model_dump.return_value={'output':[]}
            self.assertEqual(discover(svc,Mock(),args())['status'],'BLOCKED')
    def test_x_api_structured_time(self):
        a=args();a.discovery_source='x';svc=Mock();svc.reserve.return_value=None
        payload={'data':[{'id':canonical(post()['url'])[2],'text':post()['excerpt'],'created_at':'2026-10-01T19:05:40Z'}]}
        # Use current fixture time directly to avoid tying the assertion to a hand-written date.
        from ai_analysis import iso
        payload['data'][0]['created_at']=iso(NOW-60)
        with patch.dict('os.environ',{'X_BEARER_TOKEN':'fixture'}),patch('coin_discovery.time.time',return_value=NOW),patch('coin_discovery.universe',return_value={'BTC':'BTC-USDT-SWAP'}),patch('coin_discovery.fetch',return_value=json.dumps(payload)) as fetch:
            result=discover(svc,Mock(),a)
            self.assertEqual(result['status'],'observed');self.assertIn('start_time=',fetch.call_args.args[0]);self.assertNotIn('fixture',json.dumps(result))
    def test_trading_monitor_keeps_polling_during_discovery(self):
        import demo_runner
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.json';config.write_text(json.dumps({'trading':{'mode':'demo'}}))
            a=args();a.coins=str(config);a.discover_coins=True;a.ask_binance_demo_key=False;a.output=str(Path(tmp)/'signals.jsonl');a.cache_dir=tmp;a.history_days=90;a.json=False
            manager=Mock();manager.can_scan.return_value=(True,'Ready');manager.status.return_value={}
            future=Mock();future.done.return_value=False;pool=Mock();pool.submit.return_value=future
            with patch.object(demo_runner,'DemoBroker',return_value=Mock()),patch.object(demo_runner,'Journal'),patch.object(demo_runner,'Manager',return_value=manager),patch.object(demo_runner,'ThreadPoolExecutor',return_value=pool),patch.object(demo_runner.time,'sleep',side_effect=[None,None,KeyboardInterrupt]):
                demo_runner.run_trading([],a,'demo')
            self.assertEqual(manager.poll.call_count,3);self.assertEqual(pool.submit.call_count,1);manager.submit.assert_not_called()
    def test_paper_monitors_positions_absent_from_discovery(self):
        import paper_compare
        with tempfile.TemporaryDirectory() as tmp:
            a=args();a.discover_coins=True;a.output=str(Path(tmp)/'signals.jsonl');a.cache_dir=tmp;a.history_days=90
            ledger=Mock();ledger.journal.active.return_value=[{'symbol':'BTC-USDT-SWAP','created':NOW-100}];ledger.summary.return_value={}
            api=Mock();api.get.side_effect=lambda path,**kw:[{'bidPx':'1','askPx':'2','ts':str(NOW*1000)}] if path=='market/ticker' else []
            future=Mock();future.done.return_value=False;pool=Mock();pool.submit.return_value=future
            with patch.object(paper_compare,'PaperLedger',return_value=ledger),patch.object(paper_compare,'OKX',return_value=api),patch.object(paper_compare,'ThreadPoolExecutor',return_value=pool),patch.object(paper_compare.time,'sleep',side_effect=[None,None,KeyboardInterrupt]):
                paper_compare.run_comparison([],a)
            self.assertEqual(ledger.mark.call_count,3);self.assertEqual(pool.submit.call_count,1);ledger.observe.assert_not_called()
    def test_main_refreshes_every_batch(self):
        import run
        result={'status':'observed','candidates':[dict(post(),instrument='BTC-USDT-SWAP',symbol='BTC')],'time':'fixture'}
        empty={'status':'empty','candidates':[],'time':'fixture'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(run,'Service'),patch.object(run,'Collector'),patch('coin_discovery.discover',side_effect=[result,empty]) as search,patch('coin_discovery.log_discovery'),patch.object(run,'scan_batch') as scan,patch.object(run.time,'sleep',side_effect=[None,KeyboardInterrupt]):
            run.main(['--discover-coins','--watch','--no-ai','--no-events','--cache-dir',tmp])
            self.assertEqual(search.call_count,2)
            self.assertEqual(scan.call_args_list[0].args[2],['BTC-USDT-SWAP']);self.assertEqual(scan.call_args_list[1].args[2],[])
