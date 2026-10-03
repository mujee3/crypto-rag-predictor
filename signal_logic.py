"""Closed-candle trend entry; context is explanatory, execution checks are explicit."""
import math
import time
from datetime import datetime
from roi import price_for_roi

def trend_gate(snapshot,args,quote,now=None):
    now=time.time() if now is None else now
    reasons=[];warnings=[]
    def result(status,signal='WAIT',plan=None):
        return dict(status=status,signal=signal,reasons=reasons,warnings=warnings,plan=plan)
    try:
        setup=snapshot['entry_setup'];h=float(snapshot['history']['last_close']);v=float(snapshot['technical']['vwap_24h'])
        c,o,ema=map(float,(setup['close_15m'],setup['open_15m'],snapshot['technical']['ema9_15m']))
        if not all(math.isfinite(x) and x>0 for x in (h,v,c,o,ema)):raise ValueError('Invalid trend numbers')
        if not 0<=now*1000-snapshot['candle_close_ms']<=930000 or not 0<=now*1000-snapshot['history']['last_close_ms']<=3630000:
            reasons.append('Closed candles stale or future');return result('BLOCKED')
        side='LONG' if h>v and c>ema and c>o else 'SHORT' if h<v and c<ema and c<o else 'WAIT'
        if side=='WAIT':reasons.append('Hourly VWAP direction and closed 15m candle/EMA9 do not align');return result('WAIT')
        if not quote or not 0<=now*1000-quote['timestamp_ms']<=30000:
            reasons.append('Fresh executable quote unavailable');return result('BLOCKED')
        if not all(math.isfinite(float(quote[k])) for k in ('bid','ask','spread_bps','bid_depth_usdt','ask_depth_usdt')) or not 0<quote['bid']<quote['ask']:raise ValueError('Invalid book')
        if quote['spread_bps']>args.max_spread_bps:reasons.append('Spread exceeds configured maximum')
        if min(quote['bid_depth_usdt'],quote['ask_depth_usdt'])<args.min_depth:reasons.append('Observed depth insufficient')
        if reasons:return result('SKIPPED')
        if not quote['band_fully_visible']:warnings.append('Partial depth band; observed depth already meets minimum')
        if snapshot['technical'].get('relative_volume',0)<.5:warnings.append('Low recent volume; supporting context only')
        if snapshot.get('funding') and abs(snapshot['funding']['rate_per_interval'])>args.max_funding:warnings.append('Elevated funding; monitor costs')
        entry=float(quote['ask'] if side=='LONG' else quote['bid'])
        leverage=getattr(args,'leverage',1);tp=getattr(args,'take_profit_pct',25.);sl=getattr(args,'stop_loss_pct',10.)
        fee=entry*args.fee_bps/10000;exit_rate=(args.fee_bps+args.slippage_bps)/10000
        margin=entry/leverage
        target=float(price_for_roi(side,entry,1,margin,tp,fee,0,exit_rate))
        stop=float(price_for_roi(side,entry,1,margin,-sl,fee,0,exit_rate))
        if not (stop<entry<target if side=='LONG' else target<entry<stop):
            reasons.append('Costs exceed stop budget or exit levels invalid');return result('SKIPPED')
        plan=dict(reference_entry=entry,stop_reference=stop,target_reference=target,
            estimated_round_trip_cost=fee+entry*exit_rate,net_reward_risk=tp/sl,
            exit_mode='margin_roi',take_profit_pct=tp,stop_loss_pct=sl,leverage=leverage,
            note='User-selected net ROI targets, not historical resistance/support or a forecast of reachability.')
        if tp/sl<args.min_rr:reasons.append('Configured ROI reward/risk below minimum');return result('SKIPPED')
        reasons.append('Closed hourly VWAP and 15m candle/EMA9 agree')
        return result('READY',side,plan)
    except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError):
        reasons.append('Required trend/quote fields invalid');return result('BLOCKED')


def gpt_status(ai,signal,reasons):
    if ai.get('status')!='observed':return 'BLOCKED'
    if signal in ('LONG','SHORT'):return 'READY'
    if any(any(word in r.lower() for word in ('stale','unavailable','invalid','older','future')) for r in reasons):return 'BLOCKED'
    return 'WAIT' if (ai.get('assessment') or {}).get('proposal')=='WAIT' else 'SKIPPED'
