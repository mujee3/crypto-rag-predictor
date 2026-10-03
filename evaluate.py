#!/usr/bin/env python3
"""Evaluate saved one-hour signals against later observed quotes, without look-ahead.
Run --watch first. This measures endpoint direction, not stop/target execution.
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def evaluate(records, fee_bps=5., slippage_bps=3., tolerance_seconds=90):
    observations = defaultdict(list)
    invalid = 0
    for r in records:
        try:
            ref = r['evaluation_reference']
            ts, price = float(ref['timestamp_ms']), float(ref['price'])
            if not math.isfinite(ts+price) or ts <= 0 or price <= 0:
                raise ValueError('Invalid observation')
            observations[r['instrument']].append((ts, price, r))
        except (ValueError, KeyError, TypeError):
            invalid += 1
    results, pending, duplicates = [], 0, 0
    cost = 2*(fee_bps+slippage_bps)/10000
    if not math.isfinite(cost) or cost < 0 or tolerance_seconds < 0:
        raise ValueError('Costs/tolerance must be finite and nonnegative')
    for inst, obs in observations.items():
        obs.sort(key=lambda x: x[0])
        last_end = 0
        for ts, price, r in obs:
            if r.get('signal') not in ('LONG', 'SHORT', 'WAIT'):
                continue
            if ts < last_end:
                duplicates += 1
                continue
            end = ts + 3600000
            later = next((o for o in obs if end <= o[0] <= end + tolerance_seconds*1000), None)
            if later is None:
                pending += 1
                continue
            last_end = later[0]
            change = later[1]/price-1
            outcome = 'UP' if change > cost else 'DOWN' if change < -cost else 'FLAT_WITHIN_COSTS'
            side = r['signal']
            signed = change * (1 if side == 'LONG' else -1) if side != 'WAIT' else None
            results.append({'instrument': inst, 'signal': side, 'start_ms': ts, 'end_ms': later[0],
                            'return': change, 'outcome': outcome,
                            'endpoint_return_after_assumed_costs': signed-cost if signed is not None else None,
                            'direction_correct': (outcome == ('UP' if side == 'LONG' else 'DOWN')) if side != 'WAIT' else None})
    active = [x for x in results if x['signal'] != 'WAIT']
    return {'evaluated_nonoverlapping_windows': len(results), 'directional_predictions': len(active),
            'wait_windows': len(results)-len(active), 'pending_or_missing_future_quote': pending,
            'overlapping_predictions_skipped': duplicates, 'invalid_or_unavailable_records': invalid,
            'directional_hit_rate_after_cost_deadband': sum(x['direction_correct'] for x in active)/len(active) if active else None,
            'assumed_round_trip_cost_bps': cost*10000, 'windows': results,
            'limitations': ['Observed midpoint endpoint test, not executable trade P&L or stop/target backtest',
                            'No funding charges; configured cost assumptions may understate real costs',
                            'Non-overlapping windows per instrument; different coins can remain correlated',
                            'Small or selected samples do not establish predictive accuracy']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('log', nargs='?', default='signals.jsonl')
    p.add_argument('--fee-bps', type=float, default=5.)
    p.add_argument('--slippage-bps', type=float, default=3.)
    args = p.parse_args()
    rows = []
    for i, line in enumerate(Path(args.log).read_text().splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise SystemExit(f'Invalid JSON on line {i}; no evaluation produced')
    print(json.dumps(evaluate(rows, args.fee_bps, args.slippage_bps), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
