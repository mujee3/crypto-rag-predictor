#!/usr/bin/env python3
"""Public-data research signals. No exchange credentials or order execution."""
import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from crypto_http import tls_context, is_certificate_error, CERTIFICATE_HELP
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

EXTERNAL = ['macro', 'supply', 'whales', 'capital_flows', 'adoption',
            'project', 'exchange_access', 'regulation', 'security',
            'sentiment', 'event_expectations', 'liquidations', 'sector']


def utc():
    return datetime.now(timezone.utc).isoformat()


def clip(x):
    return float(np.clip(x, -1, 1))


def instrument(raw):
    raw = raw.upper().strip()
    raw = raw.replace('/USDT:USDT', '').replace('/USDT', '')
    raw = raw.removesuffix('-USDT-SWAP').removesuffix('USDT')
    if not re.fullmatch('[A-Z0-9]+', raw):
        raise ValueError('Use a ticker such as BTC, ETH or SEI, or its USDT pair')
    return raw + '-USDT-SWAP'


class OKX:
    def get(self, path, **params):
        url = 'https://www.okx.com/api/v5/' + path + '?' + urllib.parse.urlencode(params)
        error = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'crypto-research/1.0'})
                with urllib.request.urlopen(req, timeout=10, context=tls_context()) as response:
                    body = response.read()
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise ValueError('OKX returned non-JSON data; check network access or regional availability') from exc
                if data.get('code') != '0' or not data.get('data'):
                    raise ValueError(f'OKX {path}: {data.get("code")} {data.get("msg", "empty data")}')
                return data['data']
            except (OSError, ValueError) as exc:
                if is_certificate_error(exc):
                    raise RuntimeError(CERTIFICATE_HELP) from None
                error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise RuntimeError(str(error))


def candles(rows, seconds, now_ms):
    # OKX: ts,o,h,l,c,vol(contract),volCcy,volCcyQuote,confirm.
    rows = [r for r in rows if len(r) >= 9 and str(r[8]) == '1'
            and int(r[0]) + seconds * 1000 <= now_ms]
    df = pd.DataFrame([list(map(float, r[:8])) for r in rows],
                      columns=['ts', 'open', 'high', 'low', 'close', 'contracts', 'base_volume', 'quote_volume'])
    df = df.sort_values('ts').drop_duplicates('ts').set_index('ts')
    if len(df) < 100 or not np.isfinite(df.to_numpy()).all():
        raise ValueError('Insufficient or non-finite candle history')
    if (df[['open', 'high', 'low', 'close']] <= 0).any().any() or (df[['base_volume','quote_volume']] < 0).any().any():
        raise ValueError('Invalid candle prices/volume')
    if not (np.diff(df.index[-100:]) == seconds * 1000).all():
        raise ValueError('Gaps in recent candle history')
    age = now_ms - (df.index[-1] + seconds * 1000)
    if age > seconds * 1000 + 30000:
        raise ValueError('Stale candle history')
    return df


def technical(h, m):
    typical = (h.high + h.low + h.close) / 3
    vwap = float((typical * h.base_volume).iloc[-24:].sum() / h.base_volume.iloc[-24:].sum())
    tr = pd.concat([m.high - m.low, (m.high - m.close.shift()).abs(),
                    (m.low - m.close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())
    if not math.isfinite(vwap) or atr <= 0:
        raise ValueError('Invalid VWAP/ATR')
    ema9 = float(m.close.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(m.close.ewm(span=21, adjust=False).mean().iloc[-1])
    score = (.5 * clip((h.close.iloc[-1] - vwap) / (2 * atr))
             + .3 * clip((ema9 - ema21) / atr)
             + .2 * clip((m.close.iloc[-1] - m.open.iloc[-1]) / atr))
    baseline = float(m.quote_volume.iloc[-21:-1].mean())
    return score, {'vwap_24h': vwap, 'ema9_15m': ema9, 'ema21_15m': ema21,
                   'atr_15m': atr, 'relative_volume': float(m.quote_volume.iloc[-1] / baseline) if baseline else 0}


def btc_relation(m, btc):
    r = pd.concat([m.close.pct_change().rename('coin'), btc.close.pct_change().rename('btc')], axis=1).dropna().tail(192)
    if len(r) < 96 or r.btc.std() <= 1e-12 or r.coin.std() <= 1e-12:
        raise ValueError('Insufficient variable, aligned returns for BTC correlation')
    corr = float(r.coin.corr(r.btc))
    beta = float(r.coin.cov(r.btc) / r.btc.var())
    move = float((1 + r.btc.iloc[-4:]).prod() - 1)
    windows = [float(r.coin.iloc[a:b].corr(r.btc.iloc[a:b])) for a,b in [(-96,-48),(-48,None)]]
    included = (math.isfinite(corr) and all(math.isfinite(x) for x in windows)
                and abs(corr) >= .5 and all(abs(x) >= .4 and x*corr > 0 for x in windows)
                and abs(windows[0]-windows[1]) <= .25)
    score = corr * clip(move / (r.btc.std() * 2)) if included else 0.
    return score, {'correlation': corr, 'beta': beta, 'samples': len(r), 'btc_1h_return': move,
                   'window_correlations': windows, 'included': included,
                   'inclusion_reason': ('Strong, consistent recent return relationship' if included else
                                        'Excluded: weak, unstable or changing-sign return relationship'),
                   'thresholds': {'overall_abs_correlation': .5, 'window_abs_correlation': .4, 'max_window_change': .25},
                   'note': 'Two non-overlapping 12h windows plus up to 48h history; heuristic relevance, not causality'}


def book_metrics(samples, contract_value, now_ms):
    imbalances, walls, snapshots = [], [], []
    for snap in samples:
        if abs(now_ms - int(snap['ts'])) > 30000:
            raise ValueError('Stale order book')
        bids = [(float(x[0]), float(x[1]) * contract_value) for x in snap['bids']]
        asks = [(float(x[0]), float(x[1]) * contract_value) for x in snap['asks']]
        if any(not math.isfinite(p*q) or p<=0 or q<=0 for p,q in bids+asks):
            raise ValueError('Invalid order-book price or quantity')
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            raise ValueError('Empty or crossed order book')
        mid = (bids[0][0] + asks[0][0]) / 2
        near = [[(p, q) for p, q in levels if abs(p / mid - 1) <= .005] for levels in (bids, asks)]
        depth = [sum(p * q for p, q in levels) for levels in near]
        if min(depth) <= 0:
            raise ValueError('No depth within 0.5%')
        imbalances.append((depth[0] - depth[1]) / sum(depth))
        wallset = set()
        for side, levels in zip(('bid', 'ask'), near):
            sizes = [p * q for p, q in levels]
            median = float(np.median(sizes)) if sizes else 0
            for p, q in levels:
                if median > 0 and p * q >= median * 5:
                    wallset.add((side, p))
        walls.append(wallset)
        snapshots.append({'bid': bids[0][0], 'ask': asks[0][0], 'mid': mid,
                          'timestamp_ms': int(snap['ts']),
                          'spread_bps': (asks[0][0] - bids[0][0]) / mid * 10000,
                          'bid_depth_usdt': depth[0], 'ask_depth_usdt': depth[1],
                          'band_fully_visible': bids[-1][0] <= mid * .995 and asks[-1][0] >= mid * 1.005})
    common = set.intersection(*walls)
    out = snapshots[-1]
    out.update({'imbalance': float(np.mean(imbalances)), 'snapshots': len(samples),
                'persistent_walls': [{'side': s, 'price': p} for s, p in sorted(common)],
                'note': 'Sampled visible depth on OKX only. Persistence does not prove genuine intent.'})
    return clip(np.mean(imbalances)), out


def trade_flow(rows, contract_value, now_ms):
    unique = {r['tradeId']: r for r in rows if 0 <= now_ms - int(r['ts']) <= 60000}
    if len(unique) < 10:
        raise ValueError('Fewer than 10 recent trades; flow unavailable')
    totals = {'buy': 0., 'sell': 0.}
    for r in unique.values():
        price,quantity=float(r['px']),float(r['sz'])
        if not math.isfinite(price*quantity) or price<=0 or quantity<=0 or r['side'] not in totals:
            raise ValueError('Invalid trade price, size or side')
        totals[r['side']] += price * quantity * contract_value
    total = sum(totals.values())
    if total <= 0:
        raise ValueError('Empty trade notional')
    return (totals['buy'] - totals['sell']) / total, {
        'buy_usdt': totals['buy'], 'sell_usdt': totals['sell'], 'count': len(unique),
        'span_seconds': (max(int(r['ts']) for r in unique.values()) - min(int(r['ts']) for r in unique.values())) / 1000,
        'note': 'Up to latest 100 trades within 60s; may not cover the full minute'}


def trade_plan(side, m, book, atr, fee_bps, slippage_bps):
    sign = 1 if side == 'LONG' else -1
    entry = book['ask'] if sign == 1 else book['bid']
    stop = float(m.low.iloc[-8:].min() - .2 * atr) if sign == 1 else float(m.high.iloc[-8:].max() + .2 * atr)
    # Historical levels only: never extend target to manufacture acceptable R:R.
    levels = m.high.iloc[-96:] if sign == 1 else m.low.iloc[-96:]
    candidates = [float(x) for x in levels if sign * (x - entry) > .5 * atr]
    if not candidates:
        return None, 'No nearby historical target in trade direction'
    target = min(candidates) if sign == 1 else max(candidates)
    risk, reward = sign * (entry - stop), sign * (target - entry)
    costs = entry * 2 * (fee_bps + slippage_bps) / 10000
    if risk <= 0 or reward <= costs:
        return None, 'Invalid stop or target cannot cover estimated costs'
    rr = (reward - costs) / (risk + costs)
    return {'reference_entry': entry, 'stop_reference': stop, 'target_reference': target,
            'estimated_round_trip_cost': costs, 'net_reward_risk': rr,
            'note': 'Hypothetical levels, not orders. Fees/slippage are assumptions; funding excluded.'}, None

