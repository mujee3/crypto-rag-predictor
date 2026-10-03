"""Signed Binance adapter with explicit environment and read-only separation."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from crypto_http import tls_context

DEMO_HOST='https://demo-fapi.binance.com'
LIVE_HOST='https://fapi.binance.com'


class BrokerError(RuntimeError):
    def __init__(self,code=None):
        self.code=code
        super().__init__('Binance request failed'+(' (code '+str(code)+')' if code is not None else ' (unknown outcome)'))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


class DemoBroker:
    def __init__(self,mode='demo',read_only=False):
        if mode not in ('demo','live'):raise ValueError('Invalid exchange environment')
        self.mode=mode;self.host=LIVE_HOST if mode=='live' else DEMO_HOST;self.read_only=read_only
        prefix='BINANCE' if mode=='live' else 'BINANCE_DEMO'
        self.key=os.getenv(prefix+'_API_KEY')
        self.secret=os.getenv(prefix+'_API_SECRET')
        if not self.key or not self.secret:raise ValueError('Set '+prefix+'_API_KEY and '+prefix+'_API_SECRET locally or use the matching hidden credential prompt')
        self.opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()),NoRedirect())
        self.offset=0;self.info=None
        self.sync_time()

    def call(self,method,path,signed=True,**params):
        if self.read_only and method!='GET':raise ValueError('Read-only check blocks all exchange mutations')
        if signed:
            params.update(timestamp=int(time.time()*1000)+self.offset,recvWindow=5000)
        query=urllib.parse.urlencode(params)
        if signed:query+='&signature='+hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        url=self.host+path
        body=None
        if method in ('GET','DELETE'):url+='?'+query
        else:body=query.encode()
        request=urllib.request.Request(url,data=body,method=method,headers={
            **({'X-MBX-APIKEY':self.key} if signed else {}),'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with self.opener.open(request,timeout=8) as response:
                data=json.loads(response.read(4000000))
            if isinstance(data,dict) and int(data.get('code',0))<0:raise BrokerError(int(data['code']))
            return data
        except urllib.error.HTTPError as exc:
            try:code=int(json.loads(exc.read(4096)).get('code',exc.code))
            except Exception:code=exc.code
            raise BrokerError(code) from None
        except BrokerError:raise
        except Exception:raise BrokerError() from None

    def sync_time(self):
        start=int(time.time()*1000)
        result=self.call('GET','/fapi/v1/time',False)
        end=int(time.time()*1000)
        self.offset=int(result['serverTime'])-(start+end)//2

    def verify_modes(self):
        mode=self.call('GET','/fapi/v1/positionSide/dual')
        multi=self.call('GET','/fapi/v1/multiAssetsMargin')
        if mode.get('dualSidePosition') is not False or multi.get('multiAssetsMargin') is not False:
            raise ValueError('Trading requires one-way and single-asset mode; account modes are not changed automatically')

    def account(self):
        raw=self.call('GET','/fapi/v2/account')
        if raw.get('canTrade') is not True or raw.get('multiAssetsMargin') is not False:
            raise ValueError('Account cannot trade or is not single-asset mode')
        asset=next(a for a in raw['assets'] if a['asset']=='USDT')
        return {'wallet':asset['walletBalance'],'available':asset['availableBalance'],
            'initial_margin':asset['initialMargin'],
            'positions':{p['symbol']:p for p in raw['positions'] if Decimal(p['positionAmt'])!=0},
            'checked_at':time.time()}

    def orders(self):return self.call('GET','/fapi/v1/openOrders')
    def algos(self):return self.call('GET','/fapi/v1/openAlgoOrders')

    def filters(self,symbol):
        if self.info is None:self.info=self.call('GET','/fapi/v1/exchangeInfo',False)
        row=next((r for r in self.info['symbols'] if r['symbol']==symbol),None)
        if not row or row.get('status')!='TRADING' or row.get('contractType')!='PERPETUAL' or row.get('quoteAsset')!='USDT' or row.get('marginAsset')!='USDT':
            raise ValueError('No active USDT perpetual for exact symbol')
        f={x['filterType']:x for x in row['filters']}
        minimum=f.get('MIN_NOTIONAL',{}).get('notional',f.get('NOTIONAL',{}).get('minNotional'))
        if minimum is None or not Decimal(str(minimum)).is_finite() or Decimal(str(minimum))<=0:raise ValueError('Exchange minimum notional unavailable; entry blocked')
        return {'tick':f['PRICE_FILTER']['tickSize'],'min_price':f['PRICE_FILTER']['minPrice'],
            'max_price':f['PRICE_FILTER']['maxPrice'],'step':f['LOT_SIZE']['stepSize'],
            'min_qty':f['LOT_SIZE']['minQty'],'max_qty':f['LOT_SIZE']['maxQty'],
            'min_notional':minimum}

    def quote(self,symbol):
        r=self.call('GET','/fapi/v1/ticker/bookTicker',False,symbol=symbol)
        return {'bid':r['bidPrice'],'ask':r['askPrice'],'timestamp_ms':r['time']}

    def prepare(self,symbol,leverage):
        try:self.call('POST','/fapi/v1/marginType',symbol=symbol,marginType='ISOLATED')
        except BrokerError as exc:
            if exc.code!=-4046:raise
        result=self.call('POST','/fapi/v1/leverage',symbol=symbol,leverage=leverage)
        if int(result['leverage'])!=leverage:raise ValueError('Leverage was not confirmed')
        if self.mode=='live':
            rows=self.call('GET','/fapi/v2/positionRisk',symbol=symbol)
            row=next((p for p in rows if p.get('symbol')==symbol and p.get('positionSide')=='BOTH'),None)
            if not row or row.get('marginType')!='isolated' or str(row.get('isAutoAddMargin')).lower()!='false':
                raise ValueError('Live entry requires confirmed isolated margin and auto-add margin OFF')

    def enter(self,row):
        return self.call('POST','/fapi/v1/order',symbol=row['symbol'],side=row['side'],positionSide='BOTH',
            type='LIMIT',timeInForce='IOC',quantity=row['quantity'],price=row['limit_price'],
            newClientOrderId=row['entry_id'],newOrderRespType='RESULT')

    def order(self,row):return self.call('GET','/fapi/v1/order',symbol=row['symbol'],origClientOrderId=row['entry_id'])

    def cancel_entry(self,row):return self.call('DELETE','/fapi/v1/order',symbol=row['symbol'],origClientOrderId=row['entry_id'])

    def protect(self,row,kind):
        return self.call('POST','/fapi/v1/algoOrder',symbol=row['symbol'],
            side='SELL' if row['side']=='BUY' else 'BUY',positionSide='BOTH',algoType='CONDITIONAL',
            type='STOP_MARKET' if kind=='sl' else 'TAKE_PROFIT_MARKET',
            triggerPrice=row['stop'] if kind=='sl' else row['target'],closePosition='true',
            workingType='CONTRACT_PRICE',clientAlgoId=row[kind+'_id'])

    def cancel_algo(self,client_id):return self.call('DELETE','/fapi/v1/algoOrder',clientAlgoId=client_id)

    def commission_rate(self,symbol):
        value=Decimal(self.call('GET','/fapi/v1/commissionRate',symbol=symbol)['takerCommissionRate'])
        if not value.is_finite() or not 0<=value<Decimal('.1'):raise ValueError('Invalid commission rate')
        return str(value)

    def entry_commission(self,row,order):
        trades=self.call('GET','/fapi/v1/userTrades',symbol=row['symbol'],orderId=order['orderId'],limit=1000)
        own={str(t['id']):t for t in trades if str(t['orderId'])==str(order['orderId'])}
        if not own or sum((Decimal(t['qty']) for t in own.values()),Decimal(0))!=Decimal(order['executedQty']):raise ValueError('Entry fill commissions incomplete')
        if any(t['commissionAsset']!='USDT' for t in own.values()):raise ValueError('Non-USDT commissions cannot be valued reliably')
        result=sum((Decimal(t['commission']) for t in own.values()),Decimal(0))
        if not result.is_finite() or result<0:raise ValueError('Invalid entry fee')
        return result

    def funding_total(self,row,end_ms):
        start=int(row['created']*1000);seen={}
        if end_ms-start>80*86400000:raise ValueError('Funding lookback beyond supported ROI accounting window')
        while start<=end_ms:
            end=min(end_ms,start+7*86400000-1)
            for page in range(1,11):
                records=self.call('GET','/fapi/v1/income',symbol=row['symbol'],incomeType='FUNDING_FEE',startTime=start,endTime=end,limit=1000,page=page)
                for r in records:
                    if r.get('symbol')!=row['symbol'] or r.get('incomeType')!='FUNDING_FEE' or r.get('asset')!='USDT' or not start<=int(r['time'])<=end:raise ValueError('Unexpected funding record')
                    seen[str(r['tranId'])]=Decimal(r['income'])
                if len(records)<1000:break
            else:raise ValueError('Funding pagination incomplete')
            start=end+1
        total=sum(seen.values(),Decimal(0))
        if not total.is_finite():raise ValueError('Invalid funding total')
        return total

    def close(self,row,quantity):
        return self.call('POST','/fapi/v1/order',symbol=row['symbol'],side='SELL' if row['side']=='BUY' else 'BUY',
            positionSide='BOTH',type='MARKET',quantity=quantity,reduceOnly='true',
            newClientOrderId=row['close_id'],newOrderRespType='RESULT')


class LiveBroker(DemoBroker):
    def __init__(self,read_only=False):super().__init__(mode='live',read_only=read_only)
