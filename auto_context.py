"""Automatic external evidence. No user-written event scores are required.
Public feeds are partial observations, not proof that no other events exist.
"""
import concurrent.futures
import email.utils
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from crypto_http import tls_context, is_certificate_error, CERTIFICATE_HELP
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ['macro', 'supply', 'whales', 'capital_flows', 'adoption', 'project',
              'exchange_access', 'regulation', 'security', 'sentiment',
              'event_expectations', 'liquidations', 'sector']


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def stamp(value):
    if isinstance(value, (int, float)) or str(value).replace('.', '', 1).isdigit():
        x = float(value)
        return x / 1000 if x > 1e11 else x
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        dt = email.utils.parsedate_to_datetime(str(value))
    if dt.tzinfo is None:
        raise ValueError('Timestamp has no timezone')
    return dt.timestamp()


def fresh(ts, now, age):
    return 0 <= now - stamp(ts) <= age


def finite(value):
    x = float(value)
    if not math.isfinite(x):
        raise ValueError('Non-finite numeric field')
    return x


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Fetcher:
    def __init__(self, cache_dir='.crypto_cache'):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, url, ttl=300, xml=False, headers=None):
        # Hash credentials; do not put them in filenames or persisted metadata.
        identity = url + json.dumps(headers or {}, sort_keys=True)
        path = self.cache / (hashlib.sha256(identity.encode()).hexdigest() + '.json')
        now = time.time()
        try:
            old = json.loads(path.read_text())
            if 0 <= now - old['fetched_at'] < (min(ttl, 60) if old.get('error') else ttl):
                if old.get('error'):
                    raise RuntimeError(old['error'])
                return old['value']
        except (OSError, ValueError, KeyError):
            pass
        req = urllib.request.Request(url, headers={'User-Agent': 'CryptoResearch/2.0', **(headers or {})})
        try:
            handlers = [urllib.request.HTTPSHandler(context=tls_context())]
            if headers or 'api_key=' in url:
                handlers.append(NoRedirect())
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=8) as r:
                raw = r.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError('Response exceeds size limit')
            value = raw.decode('utf-8-sig') if xml else json.loads(raw)
            if not xml and isinstance(value, dict) and (value.get('error') or value.get('status') is False):
                raise ValueError('Provider returned an error response')
            # Some providers return pagination URLs containing credentials.
            secrets = list((headers or {}).values())
            secrets += urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('api_key', [])
            encoded = json.dumps(value)
            for secret in secrets:
                if secret:
                    encoded = encoded.replace(str(secret), '[REDACTED]')
                    encoded = encoded.replace(urllib.parse.quote(str(secret), safe=''), '[REDACTED]')
            value = json.loads(encoded)
            payload = {'fetched_at': now, 'value': value}
        except Exception as exc:
            # Do not echo URLs, response bodies, or secrets from exceptions.
            reason = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            payload = {'fetched_at': now, 'error': CERTIFICATE_HELP if is_certificate_error(exc) else 'Feed unavailable (' + reason + ')'}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
        if payload.get('error'):
            raise RuntimeError(payload['error'])
        return payload['value']


def resolve_coin(rows, symbol):
    matches = [r for r in rows if str(r.get('symbol') or '').upper() == symbol and r.get('is_active', True)]
    if len(matches) != 1:
        raise ValueError('Coin identity missing or ambiguous; refusing to guess')
    return matches[0]


def series_change(rows, now, value_key='tvl'):
    points = sorted([(stamp(r['date']), finite(r[value_key])) for r in rows])
    if len(points) < 2 or not fresh(points[-1][0], now, 48*3600):
        raise ValueError('Missing or stale daily series')
    latest = points[-1]
    prior = [x for x in points if 23*3600 <= latest[0]-x[0] <= 49*3600]
    if not prior or prior[-1][1] <= 0:
        raise ValueError('No usable previous daily observation')
    return {'value': latest[1], 'as_of': iso(latest[0]),
            'change': latest[1]/prior[-1][1]-1,
            'comparison_hours': (latest[0]-prior[-1][0])/3600}


def collect(symbol, fetcher, now=None):
    now = time.time() if now is None else now
    evidence, errors, blocks = {}, {}, []
    findings = {k: {'status': 'unavailable', 'evidence': [], 'score': None} for k in CATEGORIES}
    tasks = {
        'coins': lambda: fetcher.get('https://api.coinpaprika.com/v1/coins', ttl=86400),
        'chains': lambda: fetcher.get('https://api.llama.fi/v2/chains', ttl=3600),
        'stablecoins': lambda: fetcher.get('https://stablecoins.llama.fi/stablecoincharts/all', ttl=3600),
    }
    def batch(jobs):
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fn): key for key, fn in jobs.items()}
            for fut in concurrent.futures.as_completed(futures):
                key = futures[fut]
                try:
                    results[key] = fut.result()
                except Exception as exc:
                    # All exceptions from parsers contain no credentials.
                    errors[key] = str(exc)[:200]
        return results

    data = batch(tasks)
    identity = None
    if 'coins' in data:
        try:
            identity = resolve_coin(data['coins'], symbol)
            evidence['identity'] = {k: identity[k] for k in ('id', 'name', 'symbol')}
        except ValueError as exc:
            errors['identity'] = str(exc)
    # Chain symbols are accepted only when unique (no guessing between forks).
    chain = None
    if 'chains' in data:
        matches = [x for x in data['chains'] if str(x.get('tokenSymbol') or '').upper() == symbol]
        if len(matches) == 1:
            chain = matches[0]['name']
        elif len(matches) > 1:
            errors['chain_identity'] = 'Several chains use this symbol; chain-level data omitted'
    # Cross-check an ambiguous ticker against its uniquely identified native chain.
    if identity is None and chain and 'coins' in data:
        native = [r for r in data['coins'] if str(r.get('symbol') or '').upper() == symbol
                  and r.get('is_active', True) and r.get('type') == 'coin'
                  and str(r.get('name') or '').casefold() == chain.casefold()]
        if len(native) == 1:
            identity = native[0]
            errors.pop('identity', None)
            evidence['identity'] = {k: identity[k] for k in ('id', 'name', 'symbol')}
            evidence['identity']['resolved_by'] = 'Unique native chain name and coin type'
    more = {}
    if identity:
        cid = urllib.parse.quote(identity['id'], safe='')
        more['ticker'] = lambda: fetcher.get('https://api.coinpaprika.com/v1/tickers/'+cid, ttl=600)
    if chain:
        url = 'https://api.llama.fi/v2/historicalChainTvl/'+urllib.parse.quote(chain, safe='')
        more['tvl'] = lambda: fetcher.get(url, ttl=3600)
    data.update(batch(more))

    def add(category, item, status='partial'):
        findings[category]['evidence'].append(item)
        findings[category]['status'] = status

    def parse(name, fn):
        try:
            fn()
        except (ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError) as exc:
            errors[name] = 'Invalid/stale provider data: ' + str(exc)[:120]

    def supply():
        t = data['ticker']
        if str(t.get('symbol') or '').upper() != symbol or not fresh(t['last_updated'], now, 3600):
            raise ValueError('Ticker identity mismatch or stale timestamp')
        values = {k: finite(t[k]) for k in ('circulating_supply', 'total_supply', 'max_supply') if t.get(k) not in (None, '')}
        if values:
            add('supply', {'source': 'https://api.coinpaprika.com/v1/tickers/'+identity['id'],
                           'as_of': t['last_updated'], **values,
                           'limitation': 'Supply snapshot, not a vesting/unlock calendar. Free plan may omit circulating supply.'})
    if 'ticker' in data:
        parse('ticker', supply)
    def tvl():
        d = series_change(data['tvl'], now)
        add('adoption', {'source': 'https://api.llama.fi/v2/historicalChainTvl/'+urllib.parse.quote(chain),
                         'chain': chain, **d, 'limitation': 'USD TVL proxy affected by prices; not active-user or revenue measurement'})
    if 'tvl' in data:
        parse('tvl', tvl)

    def stables():
        rows = [{'date': r['date'], 'tvl': r['totalCirculatingUSD']['peggedUSD']} for r in data['stablecoins']]
        d = series_change(rows, now)
        add('capital_flows', {'source': 'https://stablecoins.llama.fi/stablecoincharts/all', **d,
            'scope': 'Aggregate USD-pegged stablecoin supply proxy; not ETF/exchange flows or money buying this coin'})
    if 'stablecoins' in data:
        parse('stablecoins', stables)

    # Optional paid feeds: keys are read locally, never requested in chat or logged.
    if os.environ.get('COINGLASS_API_KEY'):
        def liquidations():
            interval = os.environ.get('COINGLASS_INTERVAL', '4h')
            if interval not in ('30m', '1h', '4h'):
                raise ValueError('COINGLASS_INTERVAL must be 30m, 1h or 4h')
            url = 'https://open-api-v4.coinglass.com/api/futures/liquidation/aggregated-history'
            q = urllib.parse.urlencode({'exchange_list': 'Binance,OKX,Bybit', 'symbol': symbol, 'interval': interval, 'limit': 3})
            d = fetcher.get(url+'?'+q, ttl=300, headers={'CG-API-KEY': os.environ['COINGLASS_API_KEY']})
            if str(d.get('code')) != '0' or not isinstance(d.get('data'), list) or not d['data']:
                raise ValueError('No successful liquidation response')
            latest = max(d['data'], key=lambda r: stamp(r['time']))
            if not fresh(latest['time'], now, 8*3600):
                raise ValueError('Stale liquidation bucket')
            add('liquidations', {'source': url, 'interval': interval, 'bucket_time': iso(stamp(latest['time'])),
                'long_usd': finite(latest['aggregated_long_liquidation_usd']),
                'short_usd': finite(latest['aggregated_short_liquidation_usd']),
                'scope': 'Selected exchanges; bucket may still be incomplete; no directional vote'})
        try:
            liquidations()
        except Exception as exc:
            errors['coinglass'] = str(exc)[:160]
    else:
        errors['coinglass'] = 'Requires COINGLASS_API_KEY; connector available'

    if os.environ.get('TOKENOMIST_API_KEY'):
        def unlocks():
            headers = {'x-api-key': os.environ['TOKENOMIST_API_KEY']}
            tokens = fetcher.get('https://api.unlocks.app/v4/token/list', ttl=86400, headers=headers)
            token = resolve_coin(tokens['data'], symbol)
            url = 'https://api.unlocks.app/v3/unlock/events'
            q = urllib.parse.urlencode({'tokenId': token['id'], 'start': iso(now-86400)[:10], 'end': iso(now+7*86400)[:10]})
            d = fetcher.get(url+'?'+q, ttl=1800, headers=headers)
            if d.get('status') is not True or not fresh(d['metadata']['queryDate'], now, 3600):
                raise ValueError('Unlock response unavailable/stale')
            events = []
            for r in d['data']:
                if str(r.get('tokenSymbol') or '').upper() != symbol:
                    raise ValueError('Unlock identity mismatch')
                when = stamp(r['unlockDate'])
                if now-86400 <= when <= now+7*86400:
                    events.append(r)
                    if abs(when-now) < 86400 and r.get('cliffUnlocks'):
                        blocks.append('Cliff unlock scheduled within one day; timing/size may be uncertain')
            add('supply', {'source': url, 'queried_at': d['metadata']['queryDate'], 'unlock_events': events,
                           'scope': 'Provider schedule; preserve listedMethod/precision. Unlock is not proof of selling.'})
        try:
            unlocks()
        except Exception as exc:
            errors['tokenomist'] = str(exc)[:160]
    else:
        errors['tokenomist'] = 'Requires TOKENOMIST_API_KEY; connector available'

    # Whale Alert Enterprise supports some chains, not every coin.
    if os.environ.get('WHALE_ALERT_API_KEY'):
        def whales():
            base = 'https://leviathan.whale-alert.io'
            status = fetcher.get(base+'/status', ttl=86400)
            chains = [r['name'] for r in status if symbol in [s.upper() for s in r.get('symbols', [])]]
            if len(chains) != 1:
                raise ValueError('Whale chain missing/ambiguous for this coin')
            ch = urllib.parse.quote(chains[0], safe='')
            secret = urllib.parse.urlencode({'api_key': os.environ['WHALE_ALERT_API_KEY']})
            height = fetcher.get(base+'/'+ch+'/height_at_time/'+str(int(now-3600))+'?'+secret, ttl=300)['height']
            q = urllib.parse.urlencode({'start_height': height, 'symbol': symbol, 'limit': 100, 'order': 'desc'})
            d = fetcher.get(base+'/'+ch+'/transactions?'+q+'&'+secret, ttl=300)
            rows = d.get('transactions') if isinstance(d, dict) else d
            if not isinstance(rows, list):
                raise ValueError('Unexpected whale response schema')
            large = []
            for tx in rows:
                if not fresh(tx['timestamp'], now, 3600):
                    continue
                for sub in tx.get('sub_transactions', []):
                    if str(sub.get('symbol') or '').upper() != symbol or sub.get('transaction_type') != 'transfer':
                        continue
                    price = finite(sub.get('unit_price_usd', 0))
                    amount = sum(abs(finite(x['amount'])) for x in sub.get('outputs', []))*price
                    if amount >= 1_000_000:
                        large.append({'time': iso(stamp(tx['timestamp'])), 'hash': tx['hash'],
                                      'output_value_usd': amount,
                                      'input_owners': sorted({x.get('owner','unknown') for x in sub.get('inputs', [])}),
                                      'output_owners': sorted({x.get('owner','unknown') for x in sub.get('outputs', [])})})
            add('whales', {'source': base+'/'+ch+'/transactions', 'transfers': large[:20],
                'sampled_transactions': len(rows), 'truncated': bool(isinstance(d, dict) and d.get('next')) or len(rows) == 100,
                'scope': 'Latest page in last hour; not exhaustive. Change/internal transfers may remain. Not confirmed buys/sells.'})
        try:
            whales()
        except Exception as exc:
            errors['whale_alert'] = str(exc)[:160]
    else:
        errors['whale_alert'] = 'Requires WHALE_ALERT_API_KEY with Enterprise REST access; connector available'

    return {'mode': 'automatic', 'collected_at': iso(now), 'instrument': symbol+'-USDT-SWAP',
            'factors': findings, 'evidence': evidence, 'provider_errors': errors,
            'blocks': list(dict.fromkeys(blocks)),
            'missing': [k for k,v in findings.items() if v['status'] == 'unavailable'],
            'limitations': ['Every category is partial or unavailable; never a complete market picture',
                'No consensus forecasts/priced-in estimator, full social firehose, or complete capital-flow tracking',
                'Numerical observations only; major-event research is handled separately']}
