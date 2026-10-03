"""Forward comparison on observed OKX quotes; never creates an exchange order."""
import json
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from trade_manager import Journal
from roi import net_roi
from market_helpers import OKX

class PaperLedger:
    def __init__(self,path,clock=time.time):
        self.journal=Journal(path);self.clock=clock
        self.journal.db.execute('CREATE TABLE IF NOT EXISTS counts(strategy TEXT,status TEXT,reason TEXT,n INTEGER,PRIMARY KEY(strategy,status,reason))')
        self.journal.db.commit()
    def observe(self,report,args):
        for strategy,decision in report.get('comparison',{}).items():
            reason='; '.join(decision['reasons'])[:500]
            self.journal.db.execute('INSERT INTO counts VALUES(?,?,?,1) ON CONFLICT(strategy,status,reason) DO UPDATE SET n=n+1',(strategy,decision['status'],reason))
            self.journal.db.commit()
            if decision['status']!='READY' or decision['signal'] not in ('LONG','SHORT'):continue
            symbol=report['instrument'];history=[r for r in self.journal.all() if r['symbol']==symbol and r['strategy']==strategy]
            if any(r['state']=='OPEN' for r in history):continue
            if history and (report['snapshot']['candle_close_ms']<=max(r['candle'] for r in history) or self.clock()-max(r.get('closed_at',0) for r in history)<300):continue
            quote=report['post_analysis_quote']
            if not 0<=self.clock()*1000-quote['timestamp_ms']<=30000:continue
            sign=1 if decision['signal']=='LONG' else -1
            entry=float(quote['ask'] if sign==1 else quote['bid'])*(1+sign*args.slippage_bps/10000)
            margin=10.;qty=margin*args.leverage/entry
            # Both strategies share exits and equal virtual margin: compare entry policies fairly.
            row=dict(id='paper_'+uuid.uuid4().hex,symbol=symbol,strategy=strategy,state='OPEN',side=decision['signal'],entry=entry,quantity=qty,
                initial_margin=margin,entry_fee=qty*entry*args.fee_bps/10000,fee_bps=args.fee_bps,slippage_bps=args.slippage_bps,
                take_profit_pct=args.take_profit_pct,stop_loss_pct=args.stop_loss_pct,candle=report['snapshot']['candle_close_ms'],created=self.clock(),
                funding=0.,funding_events=[],funding_method='Settled public rate times observed quote notional at detection (estimate)')
            self.journal.save(row)
    def mark(self,symbol,quote,funding_events):
        now=self.clock()
        if not 0<=now*1000-int(quote['timestamp_ms'])<=30000:raise ValueError('Stale paper quote')
        if not 0<float(quote['bid'])<float(quote['ask']):raise ValueError('Invalid paper quote')
        for row in self.journal.active():
            if row['symbol']!=symbol:continue
            sign=1 if row['side']=='LONG' else -1
            for event in funding_events:
                ts=int(event['fundingTime'])
                if row['created']*1000<ts<=now*1000 and ts not in row['funding_events']:
                    rate=float(event.get('realizedRate') or event['fundingRate'])
                    row['funding']-=sign*row['quantity']*((float(quote['bid'])+float(quote['ask']))/2)*rate
                    row['funding_events'].append(ts)
            exit_price=float(quote['bid'] if sign==1 else quote['ask'])*(1-sign*row['slippage_bps']/10000)
            pnl,roi=net_roi(row['side'],row['entry'],row['quantity'],row['initial_margin'],exit_price,row['entry_fee'],row['funding'],row['fee_bps']/10000)
            gap=now-row.get('last_mark',row['created']);row['max_observation_gap_seconds']=max(gap,row.get('max_observation_gap_seconds',0))
            row.update(last_mark=now,net_roi_pct=float(roi),estimated_net_pnl=float(pnl))
            if roi>=row['take_profit_pct'] or roi<=-row['stop_loss_pct']:
                row.update(state='CLOSED',closed_at=now,exit_price=exit_price,exit_reason='TP' if roi>=row['take_profit_pct'] else 'SL')
            self.journal.save(row)
    def summary(self):
        result={}
        for strategy in ('trend','gpt'):
            rows=[r for r in self.journal.all() if r['strategy']==strategy];closed=[r for r in rows if r['state']=='CLOSED']
            result[strategy]={'opened':len(rows),'open':len(rows)-len(closed),'closed':len(closed),
                'estimated_closed_net_pnl_usdt':sum(r.get('estimated_net_pnl',0) for r in closed),
                'estimated_open_net_pnl_usdt':sum(r.get('estimated_net_pnl',0) for r in rows if r['state']=='OPEN'),
                'wins':sum(r.get('estimated_net_pnl',0)>0 for r in closed)}
        result['decision_counts']=[dict(strategy=s,status=t,reason=r,count=n) for s,t,r,n in self.journal.db.execute('SELECT * FROM counts ORDER BY n DESC')]
        result['limitation']='Equal 10-USDT virtual margins; sampled quotes, estimated slippage/funding. Not actual fills or a portfolio backtest. Gaps can miss triggers.'
        return result
    def close(self):self.journal.close()


def run_comparison(instruments,args):
    from ai_analysis import Service
    from market_numbers import Collector
    from run import scan,display
    from coin_discovery import Cycle,discover,log_discovery,attach
    root=Path(args.output).resolve().parent;root.mkdir(parents=True,exist_ok=True)
    ledger=PaperLedger(root/'paper_comparison.sqlite3');api=OKX();pool=ThreadPoolExecutor(max_workers=1)
    worker={};due={inst:0 for inst in instruments};future=None;current=None;last_summary=0
    cycle=Cycle(args) if getattr(args,"discover_coins",False) else None
    candidate=None
    def analyse(inst):
        if not worker:worker.update(service=Service(args.cache_dir),collector=Collector(args.cache_dir,args.history_days))
        if inst is None:return discover(worker['service'],worker['collector'],args)
        return scan(worker['collector'],worker['service'],inst,args)
    print('PAPER COMPARISON ONLY. No exchange credentials or orders. Ctrl+C preserves ledgers.')
    try:
        while True:
            for inst in {r['symbol'] for r in ledger.journal.active()}:
                try:
                    tick=api.get('market/ticker',instId=inst)[0]
                    # If public funding history cannot cover the oldest open trade, pause paper accounting.
                    funding=api.get('public/funding-rate-history',instId=inst,limit=100)
                    oldest=min(r['created'] for r in ledger.journal.active() if r['symbol']==inst)*1000
                    if len(funding)==100 and oldest<min(int(r['fundingTime']) for r in funding):raise ValueError('Funding history incomplete')
                    ledger.mark(inst,{'bid':tick['bidPx'],'ask':tick['askPx'],'timestamp_ms':tick['ts']},funding)
                except Exception as exc:print('PAPER MARK BLOCKED '+inst+': '+type(exc).__name__,flush=True)
            if future is not None and future.done():
                try:
                    report=future.result()
                    if cycle and current is None:
                        log_discovery(report,args);cycle.accept(report);future=None
                        continue
                    if cycle:attach(report,candidate,args)
                    display(report);ledger.observe(report,args)
                    with Path(args.output).open('a') as stream:stream.write(json.dumps(report,allow_nan=False)+'\n')
                except Exception as exc:print('PAPER SCAN BLOCKED: '+type(exc).__name__,flush=True)
                if cycle and current is None:cycle.accept({'status':'BLOCKED','candidates':[]})
                due[current]=time.time()+args.interval;future=None
            if time.time()-last_summary>=60:
                summary=ledger.summary();(root/'paper_comparison_summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
                print('PAPER SUMMARY '+json.dumps(summary),flush=True);last_summary=time.time()
            if future is None and cycle:
                candidate=cycle.next()
                if candidate:current=candidate['instrument'];future=pool.submit(analyse,current)
                elif cycle.ready():current=None;future=pool.submit(analyse,None)
            if future is None and not cycle:
                ready=[i for i in instruments if due[i]<=time.time()]
                if ready:current=min(ready,key=lambda i:due[i]);future=pool.submit(analyse,current)
            time.sleep(10)
    except KeyboardInterrupt:print('Paper comparison stopped. Restart with the same output folder to resume.')
    finally:
        (root/'paper_comparison_summary.json').write_text(json.dumps(ledger.summary(),indent=2,allow_nan=False)+'\n')
        pool.shutdown(wait=False,cancel_futures=True);ledger.close()
