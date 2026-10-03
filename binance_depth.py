"""Public Binance USDT perpetual order-book observations, not whale identities."""
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from crypto_http import tls_context, is_certificate_error, CERTIFICATE_HELP
from decimal import Decimal


def number(x):
    n = float(x)
    if not math.isfinite(n) or n <= 0:
        raise ValueError('Invalid price or quantity')
    return n


class Binance:
    def __init__(self):
        self.info = None
        self.info_time = 0

    def get(self, path, **params):
        url = 'https://fapi.binance.com/fapi/v1/' + path + '?' + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CryptoResearch/3.0'})
            with urllib.request.urlopen(req, timeout=8, context=tls_context()) as r:
                raw = r.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError('Oversized response')
            result = json.loads(raw)
            if isinstance(result, dict) and int(result.get('code', 0)) < 0:
                raise ValueError('Provider rejected market-data request')
            return result
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Binance HTTP {exc.code}; no alternate-domain retry') from None
        except (OSError, ValueError) as exc:
            if is_certificate_error(exc):
                raise RuntimeError(CERTIFICATE_HELP) from None
            raise RuntimeError('Binance unavailable or invalid response: '+type(exc).__name__) from None

    def validate_symbol(self, symbol):
        if self.info is None or time.time()-self.info_time > 3600:
            self.info = self.get('exchangeInfo')
            self.info_time = time.time()
        hits = [x for x in self.info['symbols'] if x['symbol'] == symbol]
        if len(hits) != 1 or hits[0]['status'] != 'TRADING' or hits[0]['contractType'] != 'PERPETUAL' or hits[0]['quoteAsset'] != 'USDT':
            raise ValueError('No matching active Binance USDT perpetual')


def normalize(raw, now_ms, band=.005):
    ts = int(raw.get('T') or raw.get('E') or 0)
    if not 0 <= now_ms-ts <= 30000:
        raise ValueError('Stale/future Binance book timestamp')
    sides = {}
    for side, key in [('bid', 'bids'), ('ask', 'asks')]:
        levels = []
        for p, q, *_ in raw[key]:
            price, quantity = number(p), number(q)
            levels.append({'price': price, 'quantity': quantity, 'notional_usdt': price*quantity,
                           'key': str(Decimal(str(p)).normalize())})
        levels.sort(key=lambda x:x['price'], reverse=side=='bid')
        if not levels or len({x['key'] for x in levels}) != len(levels):
            raise ValueError('Empty or duplicate Binance levels')
        sides[side] = levels
    bid, ask = sides['bid'][0]['price'], sides['ask'][0]['price']
    if bid >= ask:
        raise ValueError('Crossed Binance order book')
    mid = (bid+ask)/2
    visible = sides['bid'][-1]['price'] <= mid*(1-band) and sides['ask'][-1]['price'] >= mid*(1+band)
    levels = {s:[x for x in rows if abs(x['price']/mid-1) <= band] for s,rows in sides.items()}
    depth = {s:sum(x['notional_usdt'] for x in rows) for s,rows in levels.items()}
    if min(depth.values()) <= 0:
        raise ValueError('No depth inside configured band')
    return {'timestamp_ms':ts, 'update_id':raw['lastUpdateId'], 'mid':mid, 'bid':bid, 'ask':ask,
            'spread_bps':(ask-bid)/mid*10000, 'levels':levels, 'depth_usdt':depth,
            'band_fully_visible':visible, 'bid_share':depth['bid']/sum(depth.values()),
            'imbalance':(depth['bid']-depth['ask'])/sum(depth.values())}


def summarize(samples, min_wall=10000, multiple=5, previous=None):
    last=samples[-1]
    wallsets=[]
    for snap in samples:
        found={}
        for side, rows in snap['levels'].items():
            median=statistics.median(x['notional_usdt'] for x in rows)
            for x in rows:
                if x['notional_usdt'] >= max(min_wall, multiple*median):
                    found[(side,x['key'])]={**x,'side':side,'median_multiple':x['notional_usdt']/median}
        wallsets.append(found)
    walls=[]
    for key,w in wallsets[-1].items():
        sizes=[d[key]['notional_usdt'] for d in wallsets if key in d]
        walls.append({k:v for k,v in w.items() if k!='key'} | {
            'distance_bps':abs(w['price']/last['mid']-1)*10000,
            'wall_observations':len(sizes), 'sample_count':len(samples),
            'persistent_in_all_samples':len(sizes)==len(samples),
            'size_change_usdt':sizes[-1]-sizes[0]})
    walls.sort(key=lambda x:x['notional_usdt'], reverse=True)
    # Compare former walls to actual levels (not only the current wall threshold).
    prior=previous or samples[0]
    changes=[]
    if 0 <= last['timestamp_ms']-prior['timestamp_ms'] <= 180000:
        former=summarize([prior], min_wall, multiple)['walls'] if len(samples)>1 or previous else []
        for w in former:
            side=w['side']; rows=last['levels'][side]
            if not rows or not min(x['price'] for x in rows) <= w['price'] <= max(x['price'] for x in rows):
                status='outside_current_observed_range'; value=None
            else:
                match=next((x for x in rows if x['price']==w['price']),None)
                value=match['notional_usdt'] if match else 0
                status='unchanged' if value==w['notional_usdt'] else ('grew' if value>w['notional_usdt'] else 'reduced_or_absent')
            changes.append({'side':side,'price':w['price'],'previous_usdt':w['notional_usdt'],
                            'current_usdt':value,'observation':status})
    gaps=[]
    for side, rows in last['levels'].items():
        pairs=[(a,b,abs(a['price']-b['price'])) for a,b in zip(rows,rows[1:])]
        if pairs:
            median=statistics.median(x[2] for x in pairs)
            for a,b,gap in pairs:
                if gap>=5*median:
                    gaps.append({'side':side,'from_price':a['price'],'to_price':b['price'],
                                 'gap_bps':gap/last['mid']*10000})
    return {k:last[k] for k in ('timestamp_ms','mid','bid','ask','spread_bps','depth_usdt','bid_share','imbalance','band_fully_visible')} | {
        'walls':walls[:20], 'wall_changes':changes[:20], 'liquidity_gaps':gaps[:20],
        'sample_count':len(samples),'distinct_update_ids':len({s['update_id'] for s in samples}),
        'observed_span_seconds':(last['timestamp_ms']-samples[0]['timestamp_ms'])/1000,
        'wall_threshold':{'minimum_usdt':min_wall,'median_multiple':multiple},
        'note':'Sampled aggregate price levels, not individual accounts. Reduction may be fills, cancellations or both; snapshots cannot prove spoofing.'}


def trades_summary(rows, now_ms):
    unique={int(r['a']):r for r in rows if 0 <= now_ms-int(r['T']) <= 60000}
    if not unique:
        raise ValueError('No recent Binance aggregate trades')
    trades=[]
    for r in unique.values():
        if not isinstance(r['m'], bool):
            raise ValueError('Unexpected maker flag')
        trades.append({'price':number(r['p']),'notional_usdt':number(r['p'])*number(r['q']),
                       'aggressor':'sell' if r['m'] else 'buy','timestamp_ms':int(r['T'])})
    buy=sum(x['notional_usdt'] for x in trades if x['aggressor']=='buy')
    sell=sum(x['notional_usdt'] for x in trades if x['aggressor']=='sell')
    threshold=max(10000,5*statistics.median(x['notional_usdt'] for x in trades))
    return {'buy_usdt':buy,'sell_usdt':sell,'imbalance':(buy-sell)/(buy+sell),
            'count':len(trades),'latest_timestamp_ms':max(x['timestamp_ms'] for x in trades),
            'sample_span_seconds':(max(x['timestamp_ms'] for x in trades)-min(x['timestamp_ms'] for x in trades))/1000,
            'possibly_truncated':len(rows)>=1000,
            'large_trade_threshold_usdt':threshold,
            'large_trades':sorted([x for x in trades if x['notional_usdt']>=threshold],key=lambda x:x['notional_usdt'],reverse=True)[:20],
            'note':'Up to 1000 aggregate records in last minute; not a complete flow feed or whale identification.'}


def collect(api, symbol, count=3, interval=2, previous=None, min_wall=10000):
    report={'exchange':'Binance','market':'USDT perpetual','symbol':symbol,'status':'unavailable','errors':{}}
    last=None
    try:
        api.validate_symbol(symbol)
        samples=[]
        for i in range(count):
            samples.append(normalize(api.get('depth',symbol=symbol,limit=1000),int(time.time()*1000)))
            if i+1<count:
                time.sleep(interval)
        last=samples[-1]
        report['book']=summarize(samples,min_wall=min_wall,previous=previous)
        report['status']='partial'
        try:
            report['trades']=trades_summary(api.get('aggTrades',symbol=symbol,limit=1000),int(time.time()*1000))
            report['status']='ok'
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            report['errors']['trades']=str(exc)
        if time.time()*1000-last['timestamp_ms']>30000:
            raise ValueError('Binance book aged beyond 30 seconds during scan')
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        report['status']='unavailable'
        report['errors']['book']=str(exc)
    return report,last


def confirmation_blocks(report, side, now_ms):
    if not report or report.get('status') not in ('ok','partial'):
        return []
    b=report['book']; t=report.get('trades')
    if not 0<=now_ms-b['timestamp_ms']<=30000 or not t or not 0<=now_ms-t['latest_timestamp_ms']<=30000:
        return []
    sign=1 if side=='LONG' else -1
    if b['imbalance']*sign < -.4 and t['imbalance']*sign < -.4:
        return ['Binance nearby depth and recent trade pressure both oppose direction']
    return []
