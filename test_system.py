import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from datetime import datetime,timezone

import numpy as np
import pandas as pd
from ai_analysis import Service,Assessment,FACTORS,availability,filter_events,iso
from market_numbers import Collector,trend_summary,analogues
from market_helpers import instrument,candles
from run import gate,scan

NOW=1790906400.


def args():return SimpleNamespace(fee_bps=5.,slippage_bps=3.,min_rr=1.2,max_spread_bps=15.,min_depth=25000.,max_funding=.001,no_events=False)


def frame(n=1000):
    index=(np.arange(n)-n)*3600000+NOW*1000
    close=100+np.arange(n)*.02+np.sin(np.arange(n)/10)
    return pd.DataFrame({'open':close-.02,'high':close+.1,'low':close-.1,'close':close,
        'base_volume':np.ones(n)*100,'quote_volume':np.ones(n)*10000,'contracts':np.ones(n)*100},index=index)


def snapshot():
    return {'symbol':'SEI','instrument':'SEI-USDT-SWAP','time':iso(NOW),'candle_close_ms':NOW*1000,
        'history':{'last_close_ms':NOW*1000,'last_close':100.,'quote_volume_24h_usdt':10000.,'hourly_return_std_24h_pct':.2},
        'technical':{'atr_15m':1.,'relative_volume':1.},
        'book':{'timestamp_ms':NOW*1000,'mid':100.,'bid':99.99,'ask':100.01,'spread_bps':2.,'bid_depth_usdt':50000.,'ask_depth_usdt':50000.,'band_fully_visible':True},
        'btc':{'included':False},'breadth':{},'funding':{'rate_per_interval':.0001},
        'plans':{'LONG':{'plan':{'reference_entry':100.,'stop_reference':99.,'target_reference':102.},'error':None},
                 'SHORT':{'plan':{'reference_entry':100.,'stop_reference':101.,'target_reference':98.},'error':None}}}


def assessment(s=None,side='LONG'):
    s=s or snapshot();rows=[];avail=availability(s)
    paths={'history':'history.last_close','technical':'technical.atr_15m','volume':'history.quote_volume_24h_usdt',
           'volatility':'history.hourly_return_std_24h_pct','book':'book.mid','funding':'funding.rate_per_interval'}
    for name in FACTORS:
        effect='excluded' if avail[name]=='excluded' else 'neutral' if avail[name] else 'unavailable'
        rows.append({'name':name,'effect':effect,'reason':'Fixture observation','evidence_paths':[paths[name]] if effect=='neutral' else []})
    return Assessment(proposal=side,horizon_minutes=60,summary='Fixture proposal.',factors=rows,conflicts=[],invalidation_conditions=['Reversal'],missing_data=[])


def event(**updates):
    e={'title':'Official release','kind':'upcoming','category':'inflation','date':datetime.fromtimestamp(NOW,timezone.utc).date().isoformat(),
       'scheduled_at':iso(NOW+3600),'timing_precision':'exact','scope':'broad_market','possible_impact':'May increase volatility',
       'source_url':'https://www.bls.gov/schedule/','source_title':'Official calendar'}
    e.update(updates);return e


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)

    def service(self,c=None):return Service(self.temp.name,client=c,clock=lambda:NOW)

    def test_symbol_forms(self):
        for symbol in ('BTC','btcusdt','BTC/USDT:USDT','BTC-USDT-SWAP'):
            self.assertEqual(instrument(symbol),'BTC-USDT-SWAP')
        with self.assertRaises(ValueError):instrument('../BTC')

    def test_trends_use_real_changes(self):
        f=frame();summary=trend_summary(f)
        self.assertAlmostEqual(summary['price_change_pct']['24h'],(f.close.iloc[-1]/f.close.iloc[-25]-1)*100)
        self.assertEqual(len(summary['recent_24_hourly_candles']),24)
        self.assertEqual(trend_summary(frame(100))['price_change_pct']['30d'],None)

    def test_analogues_are_past_and_separated(self):
        f=frame();a=analogues(f)
        for m in a['matches']:
            self.assertLessEqual(m['open_time_ms'],f.index[-26])
            i=f.index.get_loc(m['open_time_ms'])
            self.assertAlmostEqual(m['next_hour_return_pct'],(f.close.iloc[i+1]/f.close.iloc[i]-1)*100)
        ts=[m['open_time_ms'] for m in a['matches']]
        self.assertTrue(all(abs(a-b)>=24*3600000 for i,a in enumerate(ts) for b in ts[i+1:]))

    def test_missing_key_no_network(self):
        with patch.dict(os.environ,{},clear=True):
            s=self.service();self.assertEqual(s.analyse(snapshot())['status'],'missing_key')
            self.assertEqual(s.events('SEI')['status'],'missing_key')

    def test_numeric_api_output_and_rate_limit(self):
        c=Mock();c.responses.parse.return_value=SimpleNamespace(output_parsed=assessment(),usage=None)
        s=self.service(c);r=s.analyse(snapshot())
        self.assertEqual(r['status'],'observed')
        payload=json.loads(c.responses.parse.call_args.kwargs['input'][1]['content'])
        self.assertNotIn('plans',payload);self.assertNotIn('trained_model',payload)
        self.assertEqual(s.analyse(snapshot())['status'],'cooldown')
        self.assertEqual(c.responses.parse.call_count,1)

    def test_evidence_catalog_paths_are_valid_and_factor_specific(self):
        from ai_analysis import evidence_catalog,leaf
        r=snapshot();r['availability']=availability(r)
        catalog=evidence_catalog(r)
        self.assertEqual(set(catalog),set(FACTORS))
        self.assertEqual(catalog['btc']['allowed_effects'],['excluded'])
        for name,entry in catalog.items():
            for path in entry['example_valid_paths']:self.assertIsNotNone(leaf(r,path))
        self.assertTrue(all(p.startswith('history.quote_volume') for p in catalog['volume']['example_valid_paths']))

    def test_wrong_citations_rejected(self):
        c=Mock();a=assessment();a.factors[0].evidence_paths=['history.invented']
        c.responses.parse.return_value=SimpleNamespace(output_parsed=a,usage=None)
        result=self.service(c).analyse(snapshot())
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['reason'],'Unknown or non-leaf evidence path: history')
        self.assertEqual(result['factor'],'history')

    def test_btc_exclusion_enforced(self):
        c=Mock();a=assessment();next(f for f in a.factors if f.name=='btc').effect='bullish'
        c.responses.parse.return_value=SimpleNamespace(output_parsed=a,usage=None)
        self.assertEqual(self.service(c).analyse(snapshot())['status'],'unavailable')

    def test_stale_core_no_paid_call(self):
        c=Mock();r=snapshot();r['book']['timestamp_ms']-=31000
        self.assertEqual(self.service(c).analyse(r)['status'],'stale_core');c.responses.parse.assert_not_called()

    def test_valid_long_and_short_risk_plans(self):
        r=snapshot()
        for side in ('LONG','SHORT'):
            ai={'status':'observed','assessment':assessment(side=side).model_dump()}
            self.assertEqual(gate(r,ai,args(),r['book'],NOW)[0],side)

    def test_gates_stale_low_depth_costs(self):
        r=snapshot();ai={'status':'observed','assessment':assessment().model_dump()}
        q=copy.deepcopy(r['book']);q['timestamp_ms']-=31000
        self.assertEqual(gate(r,ai,args(),q,NOW)[0],'WAIT')
        q=copy.deepcopy(r['book']);q['bid_depth_usdt']=10
        self.assertEqual(gate(r,ai,args(),q,NOW)[0],'WAIT')
        r['plans']['LONG']['plan']['target_reference']=100.1
        self.assertEqual(gate(r,ai,args(),r['book'],NOW)[0],'WAIT')

    def test_event_filters_unretrieved_past_and_unknown_timezone(self):
        good=event();bad=event(source_url='https://invented.example/')
        self.assertEqual(filter_events([good,bad],NOW,{good['source_url']})[1],1)
        self.assertEqual(filter_events([event(scheduled_at=iso(NOW-3600))],NOW)[0],[])
        self.assertEqual(filter_events([event(scheduled_at='2026-10-02T13:00:00')],NOW)[0],[])
        self.assertEqual(filter_events([event(timing_precision='date_only',scheduled_at=None)],NOW)[1],0)

    def test_events_require_real_search_and_cache(self):
        c=Mock();e=event()
        response=SimpleNamespace(output_text=json.dumps({'events':[e],'coverage_notes':[]}),usage=None,
            model_dump=lambda:{'output':[{'type':'web_search_call','status':'completed','action':{'sources':[{'url':e['source_url']}]}}]})
        c.responses.create.return_value=response;s=self.service(c)
        self.assertEqual(s.events('SEI')['status'],'observed')
        self.assertEqual(s.events('SEI')['status'],'cached')
        self.assertEqual(c.responses.create.call_count,1)

    def test_no_search_cannot_claim_event_evidence(self):
        c=Mock();c.responses.create.return_value=SimpleNamespace(output_text='{}',model_dump=lambda:{'output':[]})
        self.assertEqual(self.service(c).events('BTC')['status'],'unavailable')

    def test_error_secrets_not_logged(self):
        c=Mock();c.responses.parse.side_effect=RuntimeError('sk-secret-test')
        self.assertNotIn('sk-secret-test',json.dumps(self.service(c).analyse(snapshot())))

    def test_forward_history_cache_not_fake_current(self):
        f=frame(170)
        def rows(df):
            return [[str(int(t)),str(r.open),str(r.high),str(r.low),str(r.close),'100','100','10000','1'] for t,r in df.iloc[::-1].iterrows()]
        api=Mock();api.get.side_effect=lambda path,**kw:rows(f) if path=='market/candles' else []
        co=Collector(self.temp.name,7,api=api)
        data,coverage=co.history('BTC-USDT-SWAP',NOW*1000)
        self.assertEqual(len(data),168);self.assertTrue(coverage['complete_requested_window'])
        with self.assertRaises(ValueError):co.history('BTC-USDT-SWAP',(NOW+7200)*1000)

    def test_scan_core_failure_retains_events(self):
        collector=Mock();collector.collect.side_effect=ValueError('unsupported symbol')
        service=Mock();service.events.return_value={'status':'observed','events':[]}
        result=scan(collector,service,'XYZ-USDT-SWAP',args())
        self.assertEqual(result['signal'],'WAIT');self.assertEqual(result['major_events']['status'],'observed')
        service.analyse.assert_not_called()

    def test_full_collector_btc_and_sei_without_model(self):
        def as_rows(f):
            return [[str(int(t)),str(r.open),str(r.high),str(r.low),str(r.close),'100','100','10000','1'] for t,r in f.iloc[::-1].iterrows()]
        for symbol in ('BTC','SEI'):
            f=frame(200);m=frame(300);m.index=NOW*1000+(np.arange(300)-300)*900000
            def get(path,**kw):
                if path=='public/instruments':return [{'instId':kw['instId'],'state':'live','ctType':'linear','settleCcy':'USDT','ctValCcy':symbol,'ctVal':'1'}]
                if path=='market/candles':return as_rows(f if kw['bar']=='1H' else m)
                if path=='market/books':return [{'ts':str(int(NOW*1000)),'bids':[['99.99','1000'],['99','1000']],'asks':[['100.01','1000'],['101','1000']]}]
                if path=='market/trades':return [{'tradeId':str(i),'ts':str(int(NOW*1000-i*100)),'px':'100','sz':'10','side':'buy'} for i in range(20)]
                if path=='public/funding-rate':return [{'fundingRate':'.0001','ts':str(int(NOW*1000))}]
                if path=='public/open-interest':return [{'oi':'10000','ts':str(int(NOW*1000))}]
                return []
            api=Mock();api.get.side_effect=get;co=Collector(self.temp.name,7,api=api)
            with patch.object(co,'external',return_value={'factors':{},'missing':['whales']}),patch('market_numbers.collect_binance',return_value=({'status':'unavailable'},None)),patch('market_numbers.time.sleep'),patch('market_numbers.time.time',return_value=NOW):
                result=co.collect(symbol+'-USDT-SWAP',args())
            self.assertEqual(result['symbol'],symbol)
            self.assertEqual(result['history']['hourly_bars'],168)
            self.assertNotIn('trained_model',result)
            self.assertIsNotNone(result['trade_flow'])
            if symbol=='BTC':self.assertFalse(result['btc']['included'])

    def test_external_collector_requests_numbers_not_news(self):
        from auto_context import collect
        urls=[]
        class Fetcher:
            def get(self,url,**kwargs):
                urls.append(url)
                if url.endswith('/coins'):return [{'id':'btc-bitcoin','name':'Bitcoin','symbol':'BTC','is_active':True}]
                if url.endswith('/chains'):return []
                if '/tickers/' in url:return {'symbol':'BTC','last_updated':iso(NOW),'circulating_supply':20000000}
                if 'stablecoincharts' in url:return [{'date':NOW-86400,'totalCirculatingUSD':{'peggedUSD':100}},{'date':NOW,'totalCirculatingUSD':{'peggedUSD':101}}]
                raise ValueError('Unexpected endpoint')
        with patch.dict(os.environ,{},clear=True):r=collect('BTC',Fetcher(),NOW)
        self.assertEqual(r['factors']['supply']['status'],'partial')
        self.assertEqual(r['factors']['capital_flows']['status'],'partial')
        self.assertFalse(any('rss' in url or '/events' in url for url in urls))

    def test_actual_sdk_requests_with_mock_http(self):
        import httpx
        from openai import OpenAI
        captured=[]
        def handle(req):
            data=json.loads(req.content);captured.append(data)
            text=assessment().model_dump_json();output=[]
            if data.get('tools'):
                e=event();text=json.dumps({'events':[e],'coverage_notes':[]})
                output=[{'type':'web_search_call','id':'ws_test','status':'completed','action':{'type':'search','query':'calendar','sources':[{'type':'url','url':e['source_url']}]}}]
            output.append({'type':'message','role':'assistant','id':'msg_test','status':'completed','content':[{'type':'output_text','text':text,'annotations':[]}]})
            return httpx.Response(200,json={'id':'resp_test','object':'response','created_at':NOW,'status':'completed','model':'gpt-5-mini','output':output})
        c=OpenAI(api_key='local-test-only',http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        s=self.service(c)
        self.assertEqual(s.analyse(snapshot())['status'],'observed')
        self.assertEqual(s.events('SEI')['status'],'observed')
        self.assertEqual(captured[1]['tools'][0]['type'],'web_search')


if __name__=='__main__':unittest.main()
