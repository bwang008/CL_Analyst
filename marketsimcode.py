""""""
"""MC2-P1: Market simulator.

Copyright 2018, Georgia Institute of Technology (Georgia Tech)
Atlanta, Georgia 30332
All Rights Reserved

Template code for CS 4646/7646

Georgia Tech asserts copyright ownership of this template and all derivative
works, including solutions to the projects assigned in this course. Students
and other users of this template code are advised not to share it with others
or to make it available on publicly viewable websites including repositories
such as github and gitlab.  This copyright statement should not be removed
or edited.

We do grant permission to share solutions privately with non-students such
as potential employers. However, sharing with other current or future
students of CS 7646 is prohibited and subject to being investigated as a
GT honor code violation.

-----do not edit anything above this line---

Student Name: Benjamin Wang
GT User ID: bwang421 
GT ID: 903470542
"""

import datetime as dt

import numpy as np

import pandas as pd
from src.util import get_data
import io
import pdb
import math


def author():
    return 'bwang421'

def study_group():
    return 'bwang421'

def gtid():
    return '903470542'

def compute_portvals(
    df_trades,
    start_val=1000000,
    commission=0,
    impact=0,
    symbols='JPM'
):

    #modify for new marketsim code
    orders = df_trades

    #Convert index column to datetime format. Set hour to closing time 16:00 EST
    orders.index = pd.to_datetime(orders.index) #+ pd.to_timedelta(16, unit='h')
    orders = orders.sort_index(ascending=True)
    
    #pdb.set_trace()
    #syms = list(orders['Symbol'].unique())
    syms = [symbols]
    
    #Some data
    #print(orders.head())
    #print("Symbols:", syms)

    start_date = orders.index.min()
    end_date = orders.index.max()


    #[2]
    prices = get_data(syms, pd.date_range(start_date, end_date)) 
    #prices = get_data(syms, pd.date_range(start_date, end_date)).drop(columns=["SPY"])
    prices = prices[syms]


    #[3]
    shares = {symbol: 0 for symbol in syms}

    # Create columns for each symbol's shares in prices_df
    for symbol in shares:
        prices[f'Shares_{symbol}'] = 0

    prices['Cash'] = start_val
    prices['Portfolio_Value'] = 0

    #breakpoint()
    #Execute trades
    for idx_date, order in orders.iterrows():
        #Assuming only 1 symbol for the current marketsim code.
        symbol = symbols

        date = idx_date
        shares_delta = order['Shares']
        price = prices.loc[prices.index == date, symbol].values[0]

        shares[symbol] += shares_delta
        #Determine if it is buy or sell:
        if shares_delta > 0: #buy order, so impact raises price
            cost = shares_delta *price* (1+impact) +commission
            #Update portfolio value to reflect the transaction
            prices.loc[prices.index >= date, 'Cash'] -= cost
            prices.loc[prices.index >= date, f'Shares_{symbol}'] = shares[symbol]
        elif shares_delta < 0: #Sell order, so impact reduces price and commission is subtraction
            cost = shares_delta * price * (1-impact) +commission
            #Update portfolio value to reflect the transaction
            prices.loc[prices.index >= date, 'Cash'] -= cost
            prices.loc[prices.index >= date, f'Shares_{symbol}'] = shares[symbol]


    prices['Portfolio_Value'] = prices['Cash']

    # Calculate portfolio value
    for symbol in shares:
        prices['Portfolio_Value'] += prices[f'Shares_{symbol}'] * prices[symbol]

    #breakpoint()

    return prices['Portfolio_Value']

def compute_stats(portfolio_value):
    daily_return = get_daily_return(portfolio_value)
    cr = (portfolio_value[-1] / portfolio_value[0]) - 1
    adr = daily_return.mean()
    sddr = daily_return.std()
    sr = math.sqrt(252) * adr / sddr # k=252 for daily samples; risk free rate = 0

    return cr, adr, sddr, sr

def get_daily_return(portfolio_value):
    daily_return = (portfolio_value/portfolio_value.shift(1)) - 1
    daily_return.iloc[0] = 0
    return daily_return


