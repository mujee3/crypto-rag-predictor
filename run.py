#!/usr/bin/env python3
"""Numerical multi-coin analysis with GPT; optional Binance demo/live execution."""
import argparse
import getpass
import json
import math
import os
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

from ai_analysis import Service,iso
from market_helpers import instrument,book_metrics
from market_numbers import Collector,safe

ROOT=Path(__file__).resolve().parent
ALIASES={'BITCOIN':'BTC','ETHEREUM':'ETH','SOLANA':'SOL','DOGECOIN':'DOGE','SEI':'SEI'}


def gate(snapshot,ai,args,quote,now=None):
    now=time.time() if now is None else now
    reasons=[];plan=None
    side=(ai.get('assessment') or {}).get('proposal','WAIT')
    if ai.get('status')!='observed':return 'WAIT',['AI assessment unavailable: '+ai.get('status','unknown')],None
    if side=='WAIT':return 'WAIT',[ai['assessment']['summary']],None
    if side not in ('LONG','SHORT'):return 'WAIT',['Invalid AI proposal'],None
    try:
        ts=datetime.fromisoformat(snapshot['time']).timestamp()
        if not 0<=now-ts<=120:reasons.append('Numerical snapshot older than two minutes')
        if not 0<=now*1000-snapshot['candle_close_ms']<=930000:reasons.append('Closed 15-minute candle is stale or future')
        if not 0<=now*1000-snapshot['history']['last_close_ms']<=3630000:reasons.append('Hourly history is stale or future')
        if not quote or not 0<=now*1000-quote['timestamp_ms']<=30000:
            return 'WAIT',reasons+['Fresh post-analysis order book unavailable'],None
        if quote['spread_bps']>args.max_spread_bps:reasons.append('Spread exceeds configured maximum')
        if min(quote['bid_depth_usdt'],quote['ask_depth_usdt'])<args.min_depth:reasons.append('Insufficient depth within 0.5%')
        if not quote['band_fully_visible']:reasons.append('Depth band not fully visible')
        atr=snapshot['technical']['atr_15m']
        if abs(quote['mid']-snapshot['book']['mid'])>.5*atr:reasons.append('Price moved over half an ATR while AI analysed')
        if snapshot['technical']['relative_volume']<.5:reasons.append('Recent candle volume is below half its baseline')
        funding=snapshot.get('funding')
        if funding and abs(funding['rate_per_interval'])>args.max_funding:reasons.append('Extreme absolute funding rate')
        candidate=snapshot['plans'][side]
        if not candidate['plan']:return 'WAIT',reasons+[candidate['error'] or 'No historical price-level plan'],None
        original=candidate['plan'];sign=1 if side=='LONG' else -1
        entry=quote['ask'] if sign==1 else quote['bid']
        risk=sign*(entry-original['stop_reference']);reward=sign*(original['target_reference']-entry)
        cost=entry*2*(args.fee_bps+args.slippage_bps)/10000
        rr=(reward-cost)/(risk+cost) if risk>0 else -1
        if risk<=0 or reward<=cost or rr<args.min_rr:reasons.append('Net reward/risk below configured minimum or levels invalid')
        plan={**original,'reference_entry':entry,'estimated_round_trip_cost':cost,'net_reward_risk':rr,'quote_timestamp_ms':quote['timestamp_ms']}
    except (KeyError,TypeError,ValueError,ZeroDivisionError):reasons.append('Required numerical risk fields invalid')
    return ('WAIT' if reasons else side),reasons,plan


def display(report):
    print('\n'+report['symbol']+' | '+report['time']+' | '+report['signal'],flush=True)
    print('Decision: '+report.get('decision_status','WAIT')+' | Strategy: '+report.get('strategy','gpt'))
    if report.get('comparison'):
        for name,item in report['comparison'].items():print(' Comparison '+name+': '+item['status']+' / '+item['signal']+' | '+'; '.join(item['reasons']))
    for warning in report.get('context_warnings',[]):print(' Context: '+warning)
    snapshot=report.get('snapshot') or {}
    history=snapshot.get('history') or {}
    if history:
        print('History: '+str(history['hourly_bars'])+' closed hourly candles | Trend: '+history['trend'])
        print('Price changes: '+', '.join(k+': '+(f'{v:+.2f}%' if v is not None else 'unavailable') for k,v in history['price_change_pct'].items()))
        print(f"RSI(14h): {history['rsi14_1h']:.1f} | 24h volume change: {history['quote_volume_change_24h_pct'] if history['quote_volume_change_24h_pct'] is not None else 'unavailable'}%")
        coverage=snapshot['history_coverage']
        if not coverage['complete_requested_window']:print('History coverage: partial ('+str(coverage['requested_days'])+' days requested)')
    ai=report.get('ai',{});assessment=ai.get('assessment')
    print('GPT: '+ai.get('status','unavailable')+' | Proposal: '+(assessment or {}).get('proposal','unavailable'))
    if assessment:
        print(assessment['summary'])
        for factor in assessment['factors']:
            print(' '+factor['name']+' ['+factor['effect']+']: '+factor['reason'])
        if assessment['invalidation_conditions']:print('Would change the assessment: '+'; '.join(assessment['invalidation_conditions']))
    elif ai.get('reason'):
        print(' '+ai['reason'])
        if ai.get('help'):print(' '+ai['help'])
    for reason in report.get('reasons',[]):print(' - '+reason)
    plan=report.get('hypothetical_plan')
    if plan and report['signal']!='WAIT':
        print(f"Hypothetical entry {plan['reference_entry']:.8g} | stop {plan['stop_reference']:.8g} | target {plan['target_reference']:.8g} | net R/R {plan['net_reward_risk']:.2f}")
    if plan and plan.get('exit_mode')=='margin_roi':print('Whole-position exits: +'+str(plan['take_profit_pct'])+'% / -'+str(plan['stop_loss_pct'])+'% of initial margin at '+str(plan['leverage'])+'x; cost-adjusted estimates, not price forecasts.')
    external=snapshot.get('external_numbers') or {}
    if external.get('missing'):print('Missing external numbers: '+', '.join(external['missing']))
    for key,error in snapshot.get('data_errors',{}).items():print(' Data issue '+key+': '+str(error))
    events=report.get('major_events',{})
    print('\nMajor-event outlook: '+events.get('status','unavailable')+' | separate from numerical direction')
    if events.get('searched_at'):print(' Last searched: '+events['searched_at'])
    for event in events.get('events',[]):
        when=event['scheduled_at'] or event['date']+' (exact time unknown; may already have occurred if today)'
        print(' '+event['kind'].upper()+': '+event['title']+' | '+when)
        print(' Possible impact: '+event['possible_impact'])
        print(' Source: '+event['source_url'])
    if not events.get('events'):print(' No sourced events displayed; this does NOT establish that none are coming.')
    for note in events.get('coverage_notes',[]):print(' '+note)
    print('Research output; no established predictive accuracy. '+('Execution outcome shown below.' if 'execution' in report else 'No orders placed.'),flush=True)


def scan(collector,service,inst,args):
    symbol=inst.split('-')[0]
    print('Checking major scheduled events...',file=sys.stderr,flush=True)
    events={'status':'disabled','events':[],'coverage_notes':['Event search explicitly disabled.']} if args.no_events else service.events(symbol)
    print('Fetching historical and live numbers for '+symbol+'...',file=sys.stderr,flush=True)
    try:
        snapshot=collector.collect(inst,args)
    except Exception as exc:
        return {'version':13,'time':iso(time.time()),'symbol':symbol,'instrument':inst,'signal':'WAIT',
            'reasons':['Market data unavailable: '+str(exc)[:200]],'major_events':events,
            'decision_status':'BLOCKED','ai':{'status':'missing_core'},'snapshot':None,
            'comparison':{name:{'status':'BLOCKED','signal':'WAIT','reasons':['Market data unavailable'],'plan':None} for name in ('trend','gpt')}}
    print('Sending numerical evidence to GPT...',file=sys.stderr,flush=True)
    ai={'status':'disabled','reason':'GPT commentary explicitly disabled'} if getattr(args,'no_ai',False) else service.analyse(snapshot)
    fresh=None
    try:
        # Refresh after potentially slow AI inference. Contract value is validated by collector.
        info=collector.api.get('public/instruments',instType='SWAP',instId=inst)[0]
        raw=collector.api.get('market/books',instId=inst,sz=400)[0]
        fresh=book_metrics([raw],float(info['ctVal']),int(time.time()*1000))[1]
    except Exception:pass
    signal,reasons,plan=gate(snapshot,ai,args,fresh)
    from signal_logic import trend_gate,gpt_status
    comparison={'gpt':dict(status=gpt_status(ai,signal,reasons),signal=signal,reasons=reasons,plan=plan),
                'trend':trend_gate(snapshot,args,fresh)}
    strategy=getattr(args,'strategy','gpt');chosen=comparison[strategy]
    signal,reasons,plan=chosen['signal'],chosen['reasons'],chosen['plan']
    now=time.time()
    report={'version':13,'time':iso(now),'symbol':symbol,'instrument':inst,'signal':signal,
        'ai':ai,'snapshot':snapshot,'reasons':reasons,'hypothetical_plan':plan,
        'strategy':strategy,'decision_status':chosen['status'],'comparison':comparison,'context_warnings':chosen.get('warnings',[]),
        'major_events':events,'post_analysis_quote':fresh,
        'event_policy':'Warnings only; news is not converted into a directional numerical vote.'}
    if fresh:report['evaluation_reference']={'price':fresh['mid'],'timestamp_ms':fresh['timestamp_ms'],'horizon_seconds':3600}
    return safe(report)



def load_instruments(coin=None, coins_file=None):
    if coin is not None and coins_file is not None:
        raise ValueError('Use a single coin OR --coins, not both')
    if coins_file is not None:
        path=Path(coins_file)
        if path.stat().st_size>65536:
            raise ValueError('Coin-list JSON must be smaller than 64 KB')
        data=json.loads(path.read_text())
        if isinstance(data,dict):
            if 'coins' not in data or set(data)-{'coins','trading'}:raise ValueError('JSON object allows coins and optional trading only')
            data=data['coins']
        if not isinstance(data,list) or not 1<=len(data)<=20:
            raise ValueError('Supply a JSON array of 1..20 coin tickers')
    else:data=[coin or 'SEI']
    result=[]
    for item in data:
        if not isinstance(item,str) or not item.strip() or len(item)>50:
            raise ValueError('Every coin must be a nonempty ticker string, at most 50 characters')
        value=item.strip().upper()
        inst=instrument(ALIASES.get(value,value))
        if inst not in result:result.append(inst)
    return result


def scan_batch(collector,service,instruments,args):
    """Sequential fresh snapshots, isolated per-coin failures, one log record per coin."""
    reports=[]
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    batch_id=iso(time.time())
    for i,inst in enumerate(instruments,1):
        candidate=getattr(args,'discovery_candidates',{}).get(inst)
        if candidate and not 0<=time.time()-candidate['published_ts']<=args.discovery_hours*3600:continue
        symbol=inst.split('-')[0]
        print(f'\nScanning {i}/{len(instruments)}: {symbol}',file=sys.stderr,flush=True)
        try:
            report=scan(collector,service,inst,args)
            json.dumps(report,allow_nan=False)
        except Exception as exc:
            # Do not include raw provider exception bodies or potentially sensitive config.
            report={'version':13,'time':iso(time.time()),'symbol':symbol,'instrument':inst,
                'signal':'WAIT','decision_status':'BLOCKED','snapshot':None,'ai':{'status':'scan_failed'},
                'reasons':['Coin scan failed: '+type(exc).__name__],
                'major_events':{'status':'unavailable','events':[],'coverage_notes':['This coin scan did not complete.']}}
        if candidate:
            from coin_discovery import attach
            attach(report,candidate,args)
        report['batch_id']=batch_id
        if args.json:print(json.dumps(report,indent=2,allow_nan=False),flush=True)
        else:display(report)
        with output.open('a') as stream:stream.write(json.dumps(report,allow_nan=False)+'\n')
        reports.append(report)
    if len(reports)>1 and not args.json:
        print('\nBATCH SUMMARY (each coin has its own timestamp)')
        for report in reports:
            print(report['symbol']+' | '+report['signal']+' | GPT: '+report.get('ai',{}).get('status','unavailable')+' | '+report['time'])
    return reports


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('coin',nargs='?',default=None,help='Ticker such as BTC, ETH, SEI; active OKX USDT perpetual required')
    p.add_argument('--coins',metavar='FILE',help='JSON file containing {"coins": ["BTC", "ETH", "SEI"]}')
    p.add_argument('--discover-coins',action='store_true',help='Discover recent social signal candidates each cycle; ignore static coin lists')
    p.add_argument('--discovery-source',choices=['web','x'],default='web')
    p.add_argument('--discovery-hours',type=int,default=2)
    p.add_argument('--discovery-limit',type=int,default=10)
    p.add_argument('--discovery-daily-cap',type=int,default=24,help='Maximum discovery attempts per UTC day; web searches incur API costs')
    p.add_argument('--ask-api-key',action='store_true')
    p.add_argument('--watch',action='store_true',help='Repeat; default is one scan')
    p.add_argument('--once',action='store_true',help='Explicit single scan')
    p.add_argument('--interval',type=int,default=900,help='Seconds between completed batches; minimum 300')
    p.add_argument('--history-days',type=int,default=90,help='Recent hourly history, 7..365 days')
    p.add_argument('--no-events',action='store_true')
    p.add_argument('--json',action='store_true')
    p.add_argument('--output',default=str(ROOT/'signals.jsonl'))
    p.add_argument('--cache-dir',default=str(ROOT/'.numeric_cache'))
    p.add_argument('--fee-bps',type=float,default=5.)
    p.add_argument('--slippage-bps',type=float,default=3.)
    p.add_argument('--min-rr',type=float,default=1.2)
    p.add_argument('--max-spread-bps',type=float,default=15.)
    p.add_argument('--min-depth',type=float,default=25000.)
    p.add_argument('--max-funding',type=float,default=.001)
    p.add_argument('--demo-trading',action='store_true',help='Enable Binance demo execution and continuous position monitoring')
    p.add_argument('--ask-binance-demo-key',action='store_true',help='Prompt for demo credentials without saving them')
    p.add_argument('--live-trading',action='store_true',help='Enable REAL-MONEY Binance futures orders; requires live JSON')
    p.add_argument('--check-live',action='store_true',help='Read-only live account and minimum-order checks; no orders or GPT calls')
    p.add_argument('--ask-binance-key',action='store_true',help='Prompt for real Binance HMAC key and secret without saving')
    p.add_argument('--strategy',choices=['trend','gpt'],default='trend',help='Trend entry by default; GPT is explanatory context')
    p.add_argument('--no-ai',action='store_true',help='Skip optional GPT analysis; trend logic still runs')
    p.add_argument('--paper-compare',action='store_true',help='Continuously compare both strategies in paper ledgers; no exchange orders')
    p.add_argument('--leverage',type=int,default=1,help='Analysis/paper leverage; trading JSON controls execution leverage')
    p.add_argument('--take-profit-pct',type=float,default=25.)
    p.add_argument('--stop-loss-pct',type=float,default=10.)
    args=p.parse_args(argv)
    if args.paper_compare and (args.live_trading or args.demo_trading or args.check_live or args.once):p.error('Paper comparison is continuous and cannot be combined with execution or --once')
    if not 1<=args.leverage<=10 or not math.isfinite(args.take_profit_pct) or not math.isfinite(args.stop_loss_pct) or args.take_profit_pct<=0 or not 0<args.stop_loss_pct<100:p.error('Invalid leverage or ROI targets')
    if sum((args.demo_trading,args.live_trading,args.check_live))>1:p.error('Choose only one of demo trading, live trading, or read-only live check')
    if (args.live_trading or args.check_live) and (not args.coins or args.once):p.error('Live modes require --coins FILE and cannot use --once')
    if args.ask_binance_key and not (args.live_trading or args.check_live):p.error('Live credentials require --live-trading or --check-live')
    if args.check_live and (args.ask_api_key or args.watch):p.error('--check-live is one read-only check; omit --ask-api-key and --watch')
    if args.ask_binance_demo_key and not args.demo_trading:p.error('Demo credentials require --demo-trading')
    if args.demo_trading and (not args.coins or args.once):p.error('Demo trading requires --coins FILE and cannot use --once')
    if not 7<=args.history_days<=365 or args.interval<300:p.error('history-days must be 7..365; interval at least 300')
    if any(not math.isfinite(v) or v<0 for v in (args.fee_bps,args.slippage_bps,args.min_rr,args.max_spread_bps,args.min_depth,args.max_funding)):p.error('Costs and limits must be finite and nonnegative')
    if args.watch and args.once:p.error('Choose either --watch or --once')
    if args.discover_coins and (args.coin or args.check_live):p.error('Discovery cannot combine with a single coin or --check-live; use a static list for read-only sizing checks')
    if not 1<=args.discovery_hours<=24 or not 1<=args.discovery_limit<=20 or not 1<=args.discovery_daily_cap<=288:p.error('Discovery hours 1..24, limit 1..20, daily cap 1..288')
    try:instruments=[] if args.discover_coins else load_instruments(args.coin,args.coins)
    except (ValueError,OSError) as exc:p.error(str(exc))
    if args.ask_binance_key:
        if not sys.stdin.isatty():p.error('--ask-binance-key requires an interactive terminal')
        for name in ('BINANCE_API_KEY','BINANCE_API_SECRET'):
            value=getpass.getpass(name+' (hidden, not saved): ').strip()
            if not value:p.error('No credential supplied')
            os.environ[name]=value
    if args.check_live or args.live_trading:
        from trade_manager import TradeConfig
        try:
            config=TradeConfig.load(json.loads(Path(args.coins).read_text()).get('trading'))
            if config.mode!='live':raise ValueError('Live command requires trading.mode=live in JSON')
        except (ValueError,AttributeError) as exc:p.error(str(exc))
    if args.check_live:
        from live_check import check_live
        from demo_broker import BrokerError
        try:check_live(instruments,args)
        except (ValueError,OSError,BrokerError) as exc:p.error(str(exc))
        return
    if args.ask_api_key:
        if not sys.stdin.isatty():p.error('--ask-api-key requires an interactive terminal')
        key=getpass.getpass('OpenAI API key (hidden, not saved): ').strip()
        if not key:p.error('No API key supplied')
        os.environ['OPENAI_API_KEY']=key
    if args.demo_trading or args.live_trading:
        from demo_runner import run_trading
        from demo_broker import BrokerError
        try:run_trading(instruments,args,'live' if args.live_trading else 'demo')
        except (ValueError,OSError,BrokerError) as exc:p.error(str(exc))
        return
    if args.paper_compare:
        from paper_compare import run_comparison
        run_comparison(instruments,args);return
    service=Service(args.cache_dir);collector=Collector(args.cache_dir,args.history_days)
    try:
        while True:
            if args.discover_coins:
                from coin_discovery import discover,log_discovery
                result=discover(service,collector,args);log_discovery(result,args)
                args.discovery_candidates={c['instrument']:c for c in result['candidates']}
                instruments=list(args.discovery_candidates)
            scan_batch(collector,service,instruments,args)
            if not args.watch:break
            time.sleep(args.interval)
    except KeyboardInterrupt:print('\nStopped.',file=sys.stderr)


if __name__=='__main__':main()
