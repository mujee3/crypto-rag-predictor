"""Fetch and calculate market numbers. No trained model or model artifacts."""
import json
import math
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from market_helpers import OKX, candles, technical, btc_relation, book_metrics, trade_flow, trade_plan, utc
from binance_depth import Binance, collect as collect_binance
from auto_context import Fetcher, collect as collect_external

NUMERIC_CATEGORIES = ('supply', 'whales', 'capital_flows', 'adoption', 'liquidations')


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def trend_summary(frame):
    c = frame.close
    changes = {label: float((c.iloc[-1]/c.iloc[-n-1]-1)*100) if len(c)>n else None
               for label,n in [('1h',1),('4h',4),('24h',24),('7d',168),('30d',720)]}
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi = 50. if gain+loss == 0 else 100*gain/(gain+loss)
    ma20, ma50 = float(c.tail(20).mean()), float(c.tail(50).mean())
    trend = 'UP' if c.iloc[-1]>ma20>ma50 else 'DOWN' if c.iloc[-1]<ma20<ma50 else 'MIXED'
    q = frame.quote_volume
    daily_volume = float(q.tail(24).sum())
    prior_volume = float(q.iloc[-48:-24].sum())
    return safe({'source': 'OKX USDT perpetual closed 1H candles', 'hourly_bars': len(frame),
        'start_ms': int(frame.index[0]), 'last_close_ms': int(frame.index[-1]+3600000),
        'last_close': float(c.iloc[-1]), 'price_change_pct': changes,
        'sma20_1h': ma20, 'sma50_1h': ma50, 'trend_rule': 'close > SMA20 > SMA50: UP; inverse: DOWN; otherwise MIXED',
        'trend': trend, 'rsi14_1h': float(rsi), 'rsi_method': 'Simple 14-bar mean gains/losses',
        'hourly_return_std_24h_pct': float(c.pct_change().tail(24).std()*100),
        'high_24h': float(frame.high.tail(24).max()), 'low_24h': float(frame.low.tail(24).min()),
        'quote_volume_24h_usdt': daily_volume,
        'quote_volume_change_24h_pct': (daily_volume/prior_volume-1)*100 if prior_volume>0 else None,
        'recent_24_hourly_candles': [{'open_time_ms':int(t), 'open':r.open,'high':r.high,'low':r.low,
            'close':r.close,'quote_volume_usdt':r.quote_volume} for t,r in frame.tail(24).iterrows()]})


def analogues(frame):
    """Fixed descriptive nearest-period comparison. Not fitting or forecasting a model."""
    c, v = frame.close, frame.quote_volume
    x = pd.DataFrame({'return_1h':c.pct_change(), 'return_4h':c.pct_change(4),
        'return_24h':c.pct_change(24), 'volatility_24h':c.pct_change().rolling(24).std(),
        'volume_ratio':v/v.shift().rolling(24).mean().replace(0,np.nan)})
    current = x.iloc[-1]
    pool = x.iloc[:-25].replace([np.inf,-np.inf],np.nan).dropna()
    if len(pool)<100 or not np.isfinite(current).all():
        return {'status':'unavailable', 'reason':'Insufficient valid history for comparison'}
    scales = pool.std().clip(lower=1e-8)
    distances = np.sqrt((((pool-current)/scales)**2).mean(axis=1)).sort_values()
    matches=[]
    for t,distance in distances.items():
        if distance>2. or any(abs(t-m['open_time_ms'])<24*3600000 for m in matches):
            continue
        i=frame.index.get_loc(t)
        # next-hour outcome fully precedes current snapshot; choose examples at least 24h apart.
        matches.append({'open_time_ms':int(t),'distance':float(distance),
            'next_hour_return_pct':float((c.iloc[i+1]/c.iloc[i]-1)*100)})
        if len(matches)==10:break
    return {'status':'observed' if matches else 'no_close_matches', 'matches':matches,
        'comparison_fields':list(x.columns), 'method':'Fixed standardized distance; max 2, examples at least 24h apart',
        'limitation':'Descriptive selected examples, not a probability, backtest, or validated trading edge'}


class Collector:
    def __init__(self, cache, history_days=90, api=None, binance=None):
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.api=api or OKX();self.binance=binance or Binance()
        self.days=history_days;self.previous={};self.fetcher=Fetcher(str(self.cache/'providers'))

    def history(self, inst, now_ms):
        path=self.cache/(inst+'_1h.json')
        try:stored=json.loads(path.read_text())
        except (OSError,ValueError):stored=[]
        fresh=self.api.get('market/candles',instId=inst,bar='1H',limit=300)
        rows={int(r[0]):r for r in stored+fresh if len(r)>=9 and str(r[8])=='1' and int(r[0])+3600000<=now_ms}
        cutoff=(now_ms//3600000-self.days*24)*3600000
        errors=[]
        # Backfill on first use, or fill a gap after a long pause. Pagination never crosses venues.
        for _ in range(self.days*24//100+5):
            keys=sorted(rows)
            gaps=[b for a,b in zip(keys,keys[1:]) if b-a!=3600000]
            cursor=max(gaps) if gaps else (keys[0] if keys else now_ms)
            if not gaps and keys and keys[0]<=cutoff:break
            try:
                page=self.api.get('market/history-candles',instId=inst,bar='1H',after=cursor,limit=100)
                before=len(rows)
                for r in page:
                    if len(r)>=9 and str(r[8])=='1' and int(r[0])<cursor and int(r[0])+3600000<=now_ms:
                        rows[int(r[0])]=r
                if len(rows)==before:break
                time.sleep(.22)
            except Exception as exc:
                errors.append(type(exc).__name__);break
        values=[rows[k] for k in sorted(rows) if k>=cutoff]
        frame=candles(values,3600,now_ms)
        if not (np.diff(frame.index)==3600000).all():
            raise ValueError('Gapped hourly history; no interpolation or fabricated candles')
        if (frame.high < frame[['open','close','low']].max(axis=1)).any() or (frame.low > frame[['open','close','high']].min(axis=1)).any():
            raise ValueError('Invalid hourly OHLC values')
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(values));temp.replace(path)
        return frame, {'requested_days':self.days,'available_hours':len(frame),
            'complete_requested_window':len(frame)>=self.days*24,'backfill_errors':errors,
            'source':'https://www.okx.com/api/v5/market/history-candles',
            'limitation':'Requested recent window only, not full history since listing. Short listings may have less data.'}

    def external(self,symbol):
        raw=collect_external(symbol,self.fetcher)
        identity=raw.get('evidence',{}).get('identity')
        factors={name:raw['factors'][name] for name in NUMERIC_CATEGORIES}
        return {'identity':identity,'collected_at':raw.get('collected_at'), 'factors':factors,
                'missing':[k for k,v in factors.items() if v['status']=='unavailable'],
                'provider_errors':raw.get('provider_errors',{}),
                'limitations':['Daily TVL and stablecoins are proxies, not direct users/revenue or coin inflows',
                    'Whale transfers do not prove buys/sells. Paid provider coverage can be incomplete.']}

    def collect(self,inst,args):
        started=time.time();now_ms=int(started*1000);symbol=inst.split('-')[0]
        info=self.api.get('public/instruments',instType='SWAP',instId=inst)[0]
        if info.get('instId',inst)!=inst or info.get('state')!='live' or info.get('ctType')!='linear' or info.get('settleCcy')!='USDT' or info.get('ctValCcy')!=symbol:
            raise ValueError('No supported active OKX linear USDT perpetual for this exact ticker')
        ct=float(info['ctVal'])
        if ct<=0 or not math.isfinite(ct) or float(info.get('ctMult') or 1)!=1:
            raise ValueError('Unsupported contract multiplier')
        hourly,history_coverage=self.history(inst,now_ms)
        # Fetch slow histories/provider context before the short-lived book observations.
        errors={}
        def optional(name,fn):
            try:return fn()
            except Exception as exc:
                errors[name]=str(exc)[:180] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
                return None
        m=candles(self.api.get('market/candles',instId=inst,bar='15m',limit=300),900,int(time.time()*1000))
        tech_score,tech=technical(hourly,m)
        with ThreadPoolExecutor(max_workers=4) as pool:
            fext=pool.submit(optional,'external',lambda:self.external(symbol))
            fbtc=pool.submit(optional,'btc',lambda:candles(self.api.get('market/candles',instId='BTC-USDT-SWAP',bar='15m',limit=300),900,int(time.time()*1000))) if symbol!='BTC' else None
            fs={base:pool.submit(optional,base,lambda base=base:candles(self.api.get('market/candles',instId=base+'-USDT-SWAP',bar='15m',limit=120),900,int(time.time()*1000))) for base in ('ETH','SOL') if base!=symbol}
            external=fext.result();btc=fbtc.result() if fbtc else None
            breadth={}
            for base,f in fs.items():
                frame=f.result()
                if frame is not None:breadth[base]={'return_1h_pct':float((frame.close.iloc[-1]/frame.close.iloc[-5]-1)*100)}
        relation={'included':False,'inclusion_reason':'BTC is the target'} if symbol=='BTC' else {'included':False,'inclusion_reason':'Relationship unavailable'}
        if btc is not None:
            result=optional('btc_relationship',lambda:btc_relation(m,btc))
            if result:relation=result[1]
        previous=self.previous.get(inst,{})
        binance,prior_binance=collect_binance(self.binance,symbol+'USDT',count=3,interval=1,previous=previous.get('binance'),min_wall=10000)
        funding=optional('funding',lambda:self.api.get('public/funding-rate',instId=inst)[0])
        if funding:
            funding=optional('funding',lambda:self.parse_funding(funding))
        raw_oi=optional('open_interest',lambda:self.api.get('public/open-interest',instType='SWAP',instId=inst)[0])
        oi=optional('open_interest',lambda:self.parse_oi(raw_oi,previous.get('oi'))) if raw_oi else None
        samples=[]
        def fetch_book():
            for i in range(3):
                samples.append(self.api.get('market/books',instId=inst,sz=400)[0])
                if i<2:time.sleep(1)
            return book_metrics(samples,ct,int(time.time()*1000))[1]
        book=optional('book',fetch_book)
        flow_result=optional('trade_flow',lambda:trade_flow(self.api.get('market/trades',instId=inst,limit=100),ct,int(time.time()*1000)))
        flow=flow_result[1] if flow_result else None
        plans={}
        for side in ('LONG','SHORT'):
            plan,error=trade_plan(side,m,book,tech['atr_15m'],args.fee_bps,args.slippage_bps) if book else (None,'No fresh order book')
            plans[side]={'plan':plan,'error':error}
        self.previous[inst]={'oi':oi,'binance':prior_binance}
        return safe({'version':9,'time':utc(),'instrument':inst,'symbol':symbol,'venue':'OKX USDT perpetual',
            'horizon_minutes':60,'candle_close_ms':int(m.index[-1]+900000),
            'history':trend_summary(hourly),'history_coverage':history_coverage,'historical_comparisons':analogues(hourly),
            'entry_setup':{'open_15m':float(m.open.iloc[-1]),'close_15m':float(m.close.iloc[-1])},
            'technical':{**tech,'heuristic_trend_score':tech_score},'btc':relation,'breadth':breadth,
            'book':book,'trade_flow':flow,'funding':funding,'open_interest':oi,'binance':binance,
            'external_numbers':external,'plans':plans,'data_errors':errors,
            'costs':{'round_trip_fee_slippage_bps':2*(args.fee_bps+args.slippage_bps),'funding_note':'Actual funding not included; rates/holding times can differ'},
            'sources':{'market':'https://www.okx.com/api/v5/market/','funding_oi':'https://www.okx.com/api/v5/public/',
                       'binance':'https://fapi.binance.com/fapi/v1/'},
            'collection_seconds':time.time()-started,
            'limitations':['No trained prediction model. Trend and historical comparisons are descriptive.',
                'Sampled visible books/trades do not identify whales or prove genuine wall intent.',
                'No exact ETF/exchange flows, complete on-chain whales, or guaranteed liquidation coverage.']})

    @staticmethod
    def parse_funding(row):
        rate=float(row['fundingRate']);ts=int(row['ts'])
        if not math.isfinite(rate) or not 0<=time.time()*1000-ts<=300000:raise ValueError('Stale or invalid funding')
        return {'rate_per_interval':rate,'timestamp_ms':ts,'next_funding_ms':row.get('nextFundingTime')}

    @staticmethod
    def parse_oi(row,previous):
        value=float(row['oi']);ts=int(row['ts'])
        if not math.isfinite(value) or value<=0 or not 0<=time.time()*1000-ts<=300000:raise ValueError('Stale or invalid OI')
        result={'contracts':value,'timestamp_ms':ts,'note':'OI does not establish long/short direction'}
        if previous and 30<= (ts-previous['timestamp_ms'])/1000 <=1800:
            result['change_since_previous_scan']=value/previous['contracts']-1
        return result
