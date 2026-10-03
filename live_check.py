"""Read-only live account and minimum-order diagnostics; no GPT or mutations."""
import json
from pathlib import Path
from trade_manager import TradeConfig,budget,dec,quantize,textdec
from demo_broker import LiveBroker,BrokerError


def allocation_check(broker,symbol,account,config):
    filters=broker.filters(symbol);quote=broker.quote(symbol)
    bid,ask=dec(quote['bid']),dec(quote['ask'])
    if not 0<bid<ask:raise ValueError('Invalid exchange quote')
    b=budget(account,config)
    max_price=ask*(1+dec(config.max_entry_slippage_bps)/10000)
    qty=quantize(dec(b['trade_margin'])*config.leverage/max_price,filters['step'])
    minimum_qty=quantize(max(dec(filters['min_qty']),dec(filters['min_notional'])/bid),filters['step'],up=True)
    reasons=[]
    if not b['can_enter']:reasons.append('Insufficient free margin under configured cap/reserve')
    if config.max_wallet_balance_usdt is not None and dec(account['wallet'])>dec(config.max_wallet_balance_usdt):reasons.append('Wallet exceeds configured ceiling')
    if qty<=0 or qty<minimum_qty:reasons.append('3% allocation below current minimum quantity/notional; size not increased')
    if qty>dec(filters['max_qty']):reasons.append('Quantity exceeds exchange maximum')
    return {'symbol':symbol,'allocation_usdt':b['trade_margin'],'leverage':config.leverage,
        'estimated_quantity':textdec(qty),'exchange_min_notional_usdt':filters['min_notional'],
        'estimated_min_margin_usdt':textdec(minimum_qty*max_price/config.leverage),
        'size_eligible':not reasons,'reasons':reasons}


def check_live(instruments,args):
    raw=json.loads(Path(args.coins).read_text())
    config=TradeConfig.load(raw.get('trading'))
    if config.mode!='live':raise ValueError('--check-live requires trading.mode=live')
    broker=LiveBroker(read_only=True);broker.verify_modes();account=broker.account()
    orders=broker.orders();algos=broker.algos()
    results=[]
    for inst in instruments:
        symbol=inst.split('-')[0]+'USDT'
        try:
            item=allocation_check(broker,symbol,account,config)
            if symbol in account['positions'] or any(o.get('symbol')==symbol for o in orders+algos):
                item['size_eligible']=False;item['reasons'].append('Existing position/order blocks this coin')
            results.append(item)
        except (ValueError,BrokerError) as exc:results.append({'symbol':symbol,'size_eligible':False,'reason':str(exc)})
    print(json.dumps({'mode':'live_read_only','orders_placed':False,'budget':budget(account,config),
        'coins':results,'note':'Checks balances and sizing only; does not prove order permission, signal quality, or future fills.'},indent=2))
