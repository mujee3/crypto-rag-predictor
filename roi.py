"""Linear USDT-contract ROI on fixed initial margin, using Decimal accounting."""
from decimal import Decimal

def number(x):
    d=Decimal(str(x))
    if not d.is_finite():raise ValueError('Non-finite ROI input')
    return d

def net_roi(side,entry,quantity,margin,exit_price,entry_fee,funding,exit_rate):
    e,q,m,p,fee,f,r=map(number,(entry,quantity,margin,exit_price,entry_fee,funding,exit_rate))
    if side not in ('LONG','SHORT') or min(e,q,m,p)<=0 or not 0<=r<1 or fee<0:raise ValueError('Invalid ROI inputs')
    sign=1 if side=='LONG' else -1
    pnl=sign*q*(p-e)-fee+f-q*p*r
    return pnl,pnl/m*100

def price_for_roi(side,entry,quantity,margin,roi_pct,entry_fee,funding,exit_rate):
    e,q,m,target,fee,f,r=map(number,(entry,quantity,margin,roi_pct,entry_fee,funding,exit_rate))
    if side not in ('LONG','SHORT') or min(e,q,m)<=0 or not 0<=r<1 or fee<0:raise ValueError('Invalid ROI inputs')
    sign=1 if side=='LONG' else -1
    price=(target*m/100+sign*q*e+fee-f)/(q*(sign-r))
    if price<=0:raise ValueError('ROI target implies a nonpositive price')
    return price
