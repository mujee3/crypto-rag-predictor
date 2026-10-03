"""Budgeted demo trade lifecycle. Exchange state wins over local assumptions."""
import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING
from pathlib import Path
from roi import price_for_roi,net_roi


def dec(x):
    value=Decimal(str(x))
    if not value.is_finite():raise ValueError('Non-finite monetary value')
    return value


def quantize(value,step,up=False):
    value,step=dec(value),dec(step)
    if step<=0:raise ValueError('Invalid exchange price/quantity increment')
    return (value/step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*step


def textdec(x):return format(dec(x),'f')


@dataclass(frozen=True)
class TradeConfig:
    mode:str='demo'
    margin_per_trade_pct:float=3.
    max_total_margin_usdt:float=50.
    cash_reserve_usdt:float=5.
    cost_buffer_pct:float=5.
    leverage:int=1
    max_positions:int=3
    poll_seconds:int=10
    reentry_cooldown_seconds:int=300
    max_entry_slippage_bps:float=10.
    max_cross_venue_difference_bps:float=30.
    min_reward_risk:float=1.2
    fee_slippage_bps:float=16.
    max_wallet_balance_usdt:float|None=None
    max_entry_attempts:int|None=None
    strategy:str='gpt'
    exit_mode:str='price_levels'
    take_profit_pct:float=25.
    stop_loss_pct:float=10.
    exit_slippage_bps:float=3.

    @classmethod
    def load(cls,raw):
        if not isinstance(raw,dict):raise ValueError('trading must be a JSON object')
        try:c=cls(**raw)
        except TypeError:raise ValueError('Unknown trading configuration field') from None
        if c.strategy not in ('gpt','trend') or c.exit_mode not in ('price_levels','margin_roi'):raise ValueError('Invalid strategy or exit mode')
        if c.strategy=='trend' and c.exit_mode!='margin_roi':raise ValueError('Trend strategy requires margin_roi exits')
        if dec(c.take_profit_pct)<=0 or not 0<dec(c.stop_loss_pct)<100 or dec(c.exit_slippage_bps)<0:raise ValueError('Invalid ROI exits')
        if c.mode not in ('demo','live'):raise ValueError('mode must be demo or live')
        if c.mode=='live' and not {'max_total_margin_usdt','cash_reserve_usdt','leverage','max_wallet_balance_usdt','max_entry_attempts'}<=set(raw):
            raise ValueError('Live config must explicitly set margin cap, reserve, leverage, wallet ceiling and entry-attempt limit')
        if c.mode=='live' and (c.max_wallet_balance_usdt is None or c.max_entry_attempts is None):raise ValueError('Live limits cannot be null')
        if c.max_wallet_balance_usdt is not None and dec(c.max_wallet_balance_usdt)<=0:raise ValueError('Wallet ceiling must be positive')
        if c.max_entry_attempts is not None and (type(c.max_entry_attempts) is not int or c.max_entry_attempts<1):raise ValueError('Entry-attempt limit must be a positive integer')
        for name in ('leverage','max_positions','poll_seconds','reentry_cooldown_seconds'):
            if type(getattr(c,name)) is not int:raise ValueError(name+' must be an integer')
        if not 1<=c.leverage<=10 or not 1<=c.max_positions<=20 or not 5<=c.poll_seconds<=60 or c.reentry_cooldown_seconds<60:
            raise ValueError('Invalid leverage, positions, polling or cooldown limit')
        if not 0<dec(c.margin_per_trade_pct)<=100 or dec(c.max_total_margin_usdt)<=0:raise ValueError('Invalid margin limits')
        for name in ('cash_reserve_usdt','cost_buffer_pct','max_entry_slippage_bps','max_cross_venue_difference_bps','fee_slippage_bps'):
            if dec(getattr(c,name))<0:raise ValueError('Negative limit')
        if dec(c.min_reward_risk)<1:raise ValueError('Minimum reward/risk must be >=1')
        return c


def budget(account,config,reserved=0):
    wallet,available,initial=map(dec,(account['wallet'],account['available'],account['initial_margin']))
    if wallet<0 or initial<0:raise ValueError('Invalid account balance')
    # Conservatively include unavailable collateral and all account positions/orders, not only our coins.
    used=max(initial,wallet-available,Decimal(0))
    free=max(Decimal(0),min(available-dec(config.cash_reserve_usdt),
        dec(config.max_total_margin_usdt)-used)-dec(reserved))
    margin=wallet*dec(config.margin_per_trade_pct)/100
    reserve=margin*(1+dec(config.cost_buffer_pct)/100)
    return {'wallet':textdec(wallet),'used':textdec(used),'free':textdec(free),
            'trade_margin':textdec(margin),'required_with_buffer':textdec(reserve),
            'can_enter':margin>0 and reserve<=free}


def entry_plan(report,account,config,filters,quote,now):
    if report.get('signal') not in ('LONG','SHORT'):raise ValueError('Only a final LONG/SHORT signal can enter')
    if config.strategy=='trend':
        if report.get('strategy')!='trend' or report.get('decision_status')!='READY':raise ValueError('Ready trend assessment required')
    elif report.get('ai',{}).get('status')!='observed':raise ValueError('Fresh observed AI assessment required')
    if not 0<=now-datetime.fromisoformat(report['time']).timestamp()<=60:raise ValueError('Signal expired')
    if not 0<=now*1000-dec(quote['timestamp_ms'])<=10000:raise ValueError('Binance quote expired')
    bid,ask=dec(quote['bid']),dec(quote['ask'])
    if not 0<bid<ask:raise ValueError('Invalid Binance quote')
    if (ask-bid)/((ask+bid)/2)*10000>15:raise ValueError('Binance spread too wide')
    source=dec(report['hypothetical_plan']['reference_entry'])
    is_long=report['signal']=='LONG';entry=ask if is_long else bid
    if abs(entry/source-1)*10000>dec(config.max_cross_venue_difference_bps):raise ValueError('Binance and source entry differ too much')
    sign=1 if is_long else -1
    limit=quantize(entry*(1+sign*dec(config.max_entry_slippage_bps)/10000),filters['tick'],up=not is_long)
    proposed=report['hypothetical_plan']
    taker=dec(filters.get('taker_rate',config.fee_slippage_bps/20000))
    if config.exit_mode=='margin_roi':
        proposed={'stop_reference':price_for_roi(report['signal'],limit,1,limit/config.leverage,-config.stop_loss_pct,limit*taker,0,taker+dec(config.exit_slippage_bps)/10000),
                  'target_reference':price_for_roi(report['signal'],limit,1,limit/config.leverage,config.take_profit_pct,limit*taker,0,taker+dec(config.exit_slippage_bps)/10000)}
    stop=quantize(proposed['stop_reference'],filters['tick'],up=is_long)
    target=quantize(proposed['target_reference'],filters['tick'],up=is_long if config.exit_mode=='margin_roi' else not is_long)
    if not dec(filters['min_price'])<=min(limit,stop,target) or max(limit,stop,target)>dec(filters['max_price']):raise ValueError('Levels outside Binance price filters')
    risk=sign*(limit-stop);reward=sign*(target-limit);cost=limit*dec(config.fee_slippage_bps)/10000
    if risk<=0 or reward<=cost or (reward-cost)/(risk+cost)<dec(config.min_reward_risk):raise ValueError('Binance cost-adjusted reward/risk insufficient')
    b=budget(account,config)
    if not b['can_enter']:raise ValueError('Margin budget exhausted')
    # Price bound covers the maximum quote notional for either side.
    maximum_price=max(limit,ask)
    qty=quantize(dec(b['trade_margin'])*config.leverage/maximum_price,filters['step'])
    if qty<dec(filters['min_qty']) or qty<=0:raise ValueError('3% allocation below minimum quantity; not increased')
    if qty>dec(filters['max_qty']):raise ValueError('Quantity exceeds exchange maximum')
    if qty*min(limit,bid)<dec(filters['min_notional']):raise ValueError('Allocation below minimum notional; not increased')
    if qty*maximum_price/config.leverage>dec(b['trade_margin']):raise ValueError('Sizing exceeds allocated margin')
    return {'exit_mode':config.exit_mode,'take_profit_pct':config.take_profit_pct,'stop_loss_pct':config.stop_loss_pct,'taker_rate':textdec(taker),'exit_slippage_bps':config.exit_slippage_bps,'tick':filters['tick'],'min_price':filters['min_price'],'max_price':filters['max_price'],'symbol':report['symbol']+'USDT','side':'BUY' if is_long else 'SELL','quantity':textdec(qty),
        'limit_price':textdec(limit),'stop':textdec(stop),'target':textdec(target),'reserved':b['required_with_buffer'],
        'allocated_margin':b['trade_margin'],'leverage':config.leverage,'fee_slippage_bps':config.fee_slippage_bps,'min_reward_risk':config.min_reward_risk,'candle':int(report['snapshot']['candle_close_ms']),'created':now}


class Journal:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=open(str(path)+'.lock','a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();raise ValueError('Another trading manager is using this journal') from None
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY,symbol TEXT,state TEXT,payload TEXT)')
        self.db.commit()

    def bind(self,mode,key):
        # Never store the key itself; prevent accidentally reusing a journal with another key/environment.
        identity=hashlib.sha256((mode+':'+key).encode()).hexdigest()
        self.db.execute('CREATE TABLE IF NOT EXISTS identity(value TEXT NOT NULL)')
        previous=self.db.execute('SELECT value FROM identity').fetchone()
        if previous and previous[0]!=identity:raise ValueError('Journal belongs to another API key/environment; do not discard active trade history')
        if not previous:self.db.execute('INSERT INTO identity VALUES(?)',(identity,));self.db.commit()

    def save(self,row):
        self.db.execute('INSERT OR REPLACE INTO trades VALUES(?,?,?,?)',(row['id'],row['symbol'],row['state'],json.dumps(row)))
        self.db.commit()

    def active(self):return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM trades WHERE state!='CLOSED'")]
    def all(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM trades')]
    def close(self):self.db.close();self.lock.close()


class Manager:
    def __init__(self,broker,journal,config,clock=time.time):
        self.broker=broker;self.journal=journal;self.config=config;self.clock=clock
        self.account=None;self.orders=[];self.algos=[];self.healthy=False;self.last_error=None

    def reserved(self):
        return sum((dec(r['reserved']) for r in self.journal.active() if r['state']!='OPEN'),Decimal(0))

    def poll(self):
        self.healthy=False
        try:
            self.account=self.broker.account();self.orders=self.broker.orders();self.algos=self.broker.algos()
            if not isinstance(self.orders,list) or not isinstance(self.algos,list):raise ValueError('Unexpected exchange order list')
            for row in self.journal.active():self.reconcile(row)
            # Re-read after mutations; never spend against an old free-balance observation.
            self.account=self.broker.account();self.orders=self.broker.orders();self.algos=self.broker.algos()
            if not isinstance(self.orders,list) or not isinstance(self.algos,list):raise ValueError('Unexpected exchange order list')
            self.healthy=True;self.last_error=None
        except Exception as exc:self.last_error=type(exc).__name__
        return self.status()

    def status(self):
        result={'mode':self.config.mode,'healthy':self.healthy,'error':self.last_error,
                'active':[{'symbol':r['symbol'],'state':r['state'],'problem':r.get('problem'),'net_roi_pct':r.get('net_roi_pct'),'initial_margin':r.get('initial_margin'),'roi_problem':r.get('roi_problem')} for r in self.journal.active()]}
        if self.account:result['budget']=budget(self.account,self.config,self.reserved())
        return result

    def can_scan(self,symbol):
        if not self.healthy or not self.account:return False,'Account reconciliation unavailable'
        if any(r.get('roi_problem') for r in self.journal.active()):return False,'ROI accounting unavailable; monitoring only'
        if self.config.max_wallet_balance_usdt is not None and dec(self.account['wallet'])>dec(self.config.max_wallet_balance_usdt):return False,'Wallet exceeds configured ceiling; monitoring only'
        if self.config.max_entry_attempts is not None and len(self.journal.all())>=self.config.max_entry_attempts:return False,'Entry-attempt limit reached; monitoring only'
        if any(r['state']!='OPEN' for r in self.journal.active()):return False,'Pending/uncertain order or protection problem; monitoring only'
        if symbol in self.account['positions']:return False,'Position already open'
        if any(o.get('symbol')==symbol for o in self.orders+self.algos):return False,'Order already pending for coin'
        if any(r['symbol']==symbol for r in self.journal.active()):return False,'Coin lifecycle still active'
        latest=max((r.get('closed_at',0) for r in self.journal.all() if r['symbol']==symbol),default=0)
        if latest and self.clock()-latest<self.config.reentry_cooldown_seconds:return False,'Post-close cooldown'
        if len(set(self.account['positions'])|{o.get('symbol') for o in self.orders if str(o.get('reduceOnly')).lower()!='true'})>=self.config.max_positions:return False,'Maximum account positions reached'
        if not budget(self.account,self.config,self.reserved())['can_enter']:return False,'Margin budget exhausted; monitoring only'
        return True,'Ready'

    def submit(self,report):
        self.poll()
        symbol=report['symbol']+'USDT'
        ready,reason=self.can_scan(symbol)
        if not ready:return {'status':'skipped','reason':reason}
        if report.get('signal') not in ('LONG','SHORT'):return {'status':'skipped','reason':'No entry signal'}
        previous=max((r['candle'] for r in self.journal.all() if r['symbol']==symbol),default=-1)
        if int(report['snapshot']['candle_close_ms'])<=previous:return {'status':'skipped','reason':'This signal candle was already attempted'}
        try:
            filters=self.broker.filters(symbol)
            if self.config.exit_mode=='margin_roi':filters={**filters,'taker_rate':self.broker.commission_rate(symbol)}
            self.broker.prepare(symbol,self.config.leverage)
            self.poll()
            if not self.can_scan(symbol)[0]:return {'status':'skipped','reason':'Account changed before entry'}
            row=entry_plan(report,self.account,self.config,filters,self.broker.quote(symbol),self.clock())
        except ValueError as exc:return {'status':'skipped','reason':str(exc)}
        except Exception as exc:return {'status':'skipped','reason':'Broker validation failed: '+type(exc).__name__}
        identity='cn_'+uuid.uuid4().hex[:24]
        row.update(id=identity,entry_id=identity+'_e',sl_id=identity+'_s',tp_id=identity+'_t',close_id=identity+'_x',
            state='ENTRY_PENDING',sl_attempted=False,tp_attempted=False,close_attempted=False)
        self.journal.save(row)  # Durable reservation BEFORE the first entry request.
        try:self.broker.enter(row)
        except Exception:row['problem']='Entry response uncertain; never blindly resubmitted';self.journal.save(row)
        self.poll()
        return {'status':'submitted_or_pending','trade_id':identity,'state':next((r['state'] for r in self.journal.all() if r['id']==identity),'unknown')}

    def cleanup(self,row):
        remaining=self.broker.algos()
        own={row['sl_id'],row['tp_id']}
        for order in remaining:
            if order.get('clientAlgoId') in own:self.broker.cancel_algo(order['clientAlgoId'])
        remaining=self.broker.algos()
        if any(o.get('clientAlgoId') in own for o in remaining):raise ValueError('Old exit order still open')
        row.update(state='CLOSED',closed_at=self.clock(),problem=None);self.journal.save(row)

    def emergency_close(self,row,position,why):
        row.update(state='EXIT_PENDING',problem=why);self.journal.save(row)
        if not row['close_attempted']:
            # One attempt only. Uncertain close outcomes require exchange reconciliation, never repeats.
            row['close_attempted']=True;self.journal.save(row)
            self.broker.close(row,textdec(abs(dec(position['positionAmt']))))

    def reconcile(self,row):
        account=self.broker.account();position=account['positions'].get(row['symbol'])
        order=self.broker.order(row)  # Unknown entry remains reserved and blocks new entries.
        terminal=order.get('status') in ('FILLED','CANCELED','EXPIRED','REJECTED','EXPIRED_IN_MATCH')
        filled=dec(order.get('executedQty','0'))
        if not terminal:
            self.broker.cancel_entry(row)
            order=self.broker.order(row)
            terminal=order.get('status') in ('FILLED','CANCELED','EXPIRED','REJECTED','EXPIRED_IN_MATCH')
            if not terminal:return
            filled=dec(order.get('executedQty','0'))
            position=self.broker.account()['positions'].get(row['symbol'])
        position=self.broker.account()['positions'].get(row['symbol'])
        if position is None:
            # A fresh account read after confirmed terminal entry, then remove only our exits.
            if filled and self.clock()-row['created']<5:return
            self.cleanup(row);return
        qty=dec(position['positionAmt']);expected_sign=1 if row['side']=='BUY' else -1
        if qty*expected_sign<=0 or abs(qty)!=filled:
            row.update(state='MANUAL_REVIEW',problem='Position changed outside bot or fill mismatch; no automatic close')
            self.journal.save(row);return
        if row['state']=='MANUAL_REVIEW':return
        if row['state']=='EXIT_PENDING':return  # Existing close outcome must be resolved; do not duplicate.
        exits={o.get('clientAlgoId'):o for o in self.broker.algos()}
        try:
            fill=dec(order['avgPrice']);direction=1 if row['side']=='BUY' else -1
            if row.get('exit_mode')=='margin_roi' and 'initial_margin' not in row:
                margin=filled*fill/dec(row['leverage']);fee=filled*fill*dec(row['taker_rate'])
                rate=dec(row['taker_rate'])+dec(row['exit_slippage_bps'])/10000
                side='LONG' if direction==1 else 'SHORT'
                row['initial_margin']=textdec(margin);row['fill_price']=textdec(fill);row['filled_quantity']=textdec(filled)
                row['stop']=textdec(quantize(price_for_roi(side,fill,filled,margin,-row['stop_loss_pct'],fee,0,rate),row['tick'],up=direction==1))
                row['target']=textdec(quantize(price_for_roi(side,fill,filled,margin,row['take_profit_pct'],fee,0,rate),row['tick'],up=direction==1))
                if not dec(row['min_price'])<=min(dec(row['stop']),dec(row['target'])) or max(dec(row['stop']),dec(row['target']))>dec(row['max_price']):raise ValueError('ROI levels outside exchange filters')
                self.journal.save(row)
            risk=direction*(fill-dec(row['stop']));reward=direction*(dec(row['target'])-fill)
            cost=fill*dec(row.get('fee_slippage_bps',self.config.fee_slippage_bps))/10000
            if fill<=0 or filled*fill/dec(row.get('leverage',self.config.leverage))>dec(row['allocated_margin']) or risk<=0 or (reward-cost)/(risk+cost)<dec(row.get('min_reward_risk',self.config.min_reward_risk)):
                raise ValueError('Actual fill violates sizing or reward/risk limits')
            for kind in ('sl','tp'):
                if row[kind+'_id'] not in exits:
                    if row[kind+'_attempted']:
                        self.emergency_close(row,position,'Protective order missing after its placement attempt');return
                    row[kind+'_attempted']=True;row['state']='PROTECTING';self.journal.save(row)
                    self.broker.protect(row,kind)
                    exits={o.get('clientAlgoId'):o for o in self.broker.algos()}
                    if row[kind+'_id'] not in exits:raise ValueError('Protection not confirmed')
                actual=exits[row[kind+'_id']]
                if actual.get('symbol')!=row['symbol'] or actual.get('side')!=('SELL' if row['side']=='BUY' else 'BUY') or str(actual.get('closePosition')).lower()!='true' or actual.get('orderType')!=('STOP_MARKET' if kind=='sl' else 'TAKE_PROFIT_MARKET') or dec(actual.get('triggerPrice','0'))!=dec(row['stop'] if kind=='sl' else row['target']) or actual.get('workingType')!='CONTRACT_PRICE' or actual.get('positionSide')!='BOTH':
                    raise ValueError('Protective order parameters mismatch')
            row.update(state='OPEN',problem=None);self.journal.save(row)
        except Exception:
            # If an exit filled while setting the other one, do not send a new opening/reversing order.
            position=self.broker.account()['positions'].get(row['symbol'])
            if position is None:self.cleanup(row)
            else:self.emergency_close(row,position,'Protection could not be verified; attempted reduce-only exit')
            return
        if row.get('exit_mode')=='margin_roi':self.monitor_roi(row,order,position)

    def monitor_roi(self,row,order,position):
        try:
            if 'entry_fee' not in row:
                row['entry_fee']=textdec(self.broker.entry_commission(row,order))
            now=self.clock()
            if now-row.get('funding_checked_at',0)>=60:
                row['funding_total']=textdec(self.broker.funding_total(row,int(now*1000)))
                row['funding_checked_at']=now
            quote=self.broker.quote(row['symbol']);now=self.clock()
            if not 0<=now*1000-dec(quote['timestamp_ms'])<=10000:raise ValueError('Stale ROI quote')
            side='LONG' if row['side']=='BUY' else 'SHORT'
            price=quote['bid'] if side=='LONG' else quote['ask']
            rate=dec(row['taker_rate'])+dec(row['exit_slippage_bps'])/10000
            pnl,roi=net_roi(side,row['fill_price'],row['filled_quantity'],row['initial_margin'],price,row['entry_fee'],row['funding_total'],rate)
            row.update(net_roi_pct=textdec(roi),estimated_net_pnl=textdec(pnl),roi_problem=None)
            self.journal.save(row)
            if roi>=dec(row['take_profit_pct']) or roi<=-dec(row['stop_loss_pct']):
                self.emergency_close(row,position,'Whole-position net ROI exit threshold reached')
        except Exception as exc:
            row['roi_problem']='Net ROI accounting unavailable: '+type(exc).__name__+'; exchange backup exits remain'
            self.journal.save(row)
