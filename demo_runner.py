"""Keep account monitoring on the main thread while one numerical scan runs."""
import getpass
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ai_analysis import Service,iso
from market_numbers import Collector
from demo_broker import DemoBroker,LiveBroker
from trade_manager import TradeConfig,Journal,Manager
from live_check import allocation_check


def run_demo(instruments,args):
    return run_trading(instruments,args,'demo')


def run_trading(instruments,args,mode):
    from run import scan,display
    from coin_discovery import Cycle,discover,log_discovery,attach
    raw=json.loads(Path(args.coins).read_text())
    if not isinstance(raw,dict) or 'trading' not in raw:
        raise ValueError('Trading requires a trading object in the coin JSON')
    config=TradeConfig.load(raw['trading'])
    args.strategy=config.strategy;args.leverage=config.leverage
    args.take_profit_pct=config.take_profit_pct;args.stop_loss_pct=config.stop_loss_pct
    if config.mode!=mode:raise ValueError('Command and JSON trading mode must match')
    if args.ask_binance_demo_key:
        if not sys.stdin.isatty():raise ValueError('Credential prompts require an interactive terminal')
        for name in ('BINANCE_DEMO_API_KEY','BINANCE_DEMO_API_SECRET'):
            os.environ[name]=getpass.getpass(name+' (hidden, not saved): ').strip()
    journal=Journal(Path(__file__).resolve().parent/(mode+'_trades.sqlite3'))
    pool=ThreadPoolExecutor(max_workers=1)
    try:
        broker=LiveBroker() if mode=='live' else DemoBroker()
        broker.verify_modes()
        if hasattr(broker,'key'):journal.bind(mode,broker.key)
        manager=Manager(broker,journal,config)
        worker_state={}
        cycle=Cycle(args) if getattr(args,"discover_coins",False) else None
        candidate=None
        def analyse(inst):
            # SQLite connections belong to their creating thread.
            if not worker_state:
                worker_state.update(service=Service(args.cache_dir),collector=Collector(args.cache_dir,args.history_days))
            if inst is None:return discover(worker_state['service'],worker_state['collector'],args)
            return scan(worker_state['collector'],worker_state['service'],inst,args)
        due={inst:0 for inst in instruments};future=None;current=None;last_status=None;last_print=0
        output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
        print(('LIVE: REAL MONEY ORDERS ENABLED.' if mode=='live' else 'DEMO ONLY.')+' Ctrl+C stops monitoring; exchange exit orders remain active.',file=sys.stderr)
        while True:
            status=manager.poll()
            if future is not None and future.done():
                try:
                    report=future.result()
                    if cycle and current is None:
                        log_discovery(report,args);cycle.accept(report);future=None
                        continue
                    if cycle:attach(report,candidate,args)
                    report['execution']=manager.submit(report)
                    if report['execution'].get('status')=='skipped' and report.get('signal') in ('LONG','SHORT'):report['decision_status']='SKIPPED'
                    if args.json:print(json.dumps(report,allow_nan=False),flush=True)
                    else:
                        display(report)
                        print(mode.upper()+' execution: '+json.dumps(report['execution']),flush=True)
                    with output.open('a') as stream:stream.write(json.dumps(report,allow_nan=False)+'\n')
                except Exception as exc:
                    print(mode.upper()+' scan/entry unavailable: '+type(exc).__name__+'; monitoring continues.',file=sys.stderr)
                if cycle and current is None:cycle.accept({'status':'BLOCKED','candidates':[]})
                due[current]=time.time()+args.interval;future=None
            reasons={inst:manager.can_scan(inst.split('-')[0]+'USDT')[1] for inst in instruments}
            summary={'account':manager.status(),'coins':reasons}
            if summary!=last_status or time.time()-last_print>=60:
                print(mode.upper()+' MONITOR '+json.dumps(summary),file=sys.stderr,flush=True)
                last_status=summary;last_print=time.time()
            if future is None and cycle:
                candidate=cycle.next()
                if candidate:
                    current=candidate['instrument']
                    symbol=candidate['symbol']+'USDT'
                    ready,reason=manager.can_scan(symbol)
                    if not ready:print(mode.upper()+' DISCOVERY SKIP '+symbol+': '+reason,file=sys.stderr,flush=True)
                    if ready:
                        try:
                            sizing=allocation_check(broker,symbol,manager.account,config)
                            if sizing['size_eligible']:future=pool.submit(analyse,current)
                            else:print(mode.upper()+' SIZE SKIP '+json.dumps(sizing),file=sys.stderr,flush=True)
                        except Exception as exc:print('Sizing unavailable: '+type(exc).__name__,file=sys.stderr)
                elif cycle.ready() and manager.can_scan('__DISCOVERY__')[0]:
                    current=None;future=pool.submit(analyse,None)
            if future is None and not cycle:
                eligible=[inst for inst in instruments if due[inst]<=time.time() and manager.can_scan(inst.split('-')[0]+'USDT')[0]]
                if eligible:
                    current=min(eligible,key=lambda inst:due[inst])
                    try:
                        sizing=allocation_check(broker,current.split('-')[0]+'USDT',manager.account,config)
                        if sizing['size_eligible']:future=pool.submit(analyse,current)
                        else:
                            print(mode.upper()+' SIZE SKIP '+json.dumps(sizing),file=sys.stderr,flush=True)
                            due[current]=time.time()+args.interval
                    except Exception as exc:
                        print('Sizing unavailable: '+type(exc).__name__,file=sys.stderr)
                        due[current]=time.time()+args.interval
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print('\nStopped. Check the '+mode+' account: positions and exchange TP/SL orders remain. Restart with the same journal to resume monitoring.',file=sys.stderr)
    finally:
        pool.shutdown(wait=False,cancel_futures=True)
        journal.close()
