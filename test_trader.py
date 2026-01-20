#main helper function to run the project and collect data

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pandas_ta as ta
#import sklearn as sk
import datetime as dt

import src.StrategyLearner as sl
import src.StrategyQLearner as sql
import src.LearnerBag as lbg
import src.LearnerRT as lrt
import os
import yfinance as yf
import src.util as ut
import marketsimcode as msc

#import yfinance as yf

def create_cl_data():
    cols=['Day','Time','Open','High','Low','Close','Volume']
    cl = pd.read_csv('data/test.csv', sep=';', index_col=None, header=None, names=cols) #initial dataset 100 records to get basic functionality working.
    #cl = pd.read_csv('data/test10k.csv') #larger dataset to test with 
    
    # Create date column by combining Day + Time. Set it as the index.
    cl['Date'] = pd.to_datetime(cl['Day'] + ' ' + cl['Time'],
                                 format='%d/%m/%Y %H:%M')
    cl = cl.set_index('Date')
    
    #Drop Day + Time
    cl = cl.drop(['Day','Time'],axis=1)
    
    #This is only separated by 5min, if we want to do more granular data, we need to use pandas resample.
    
    return cl

def resample_data(cl_5m):
    # --- Resampling 5min ticker data ---

    # Define the aggregation rules for OHLCV data
    ohlcv_agg = {
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }

    # Resample to different timeframes
    # 'D' for Daily, 'B' for Business Day (Mon-Fri) - choose 'D' if your data includes weekends
    cl_1d = cl_5m.resample('D').agg(ohlcv_agg) 
    # '2D' for 2-Day periods
    cl_2d = cl_5m.resample('2D').agg(ohlcv_agg)
    # 'W' for Weekly (ends on Sunday by default)
    cl_1w = cl_5m.resample('W').agg(ohlcv_agg) 
    # 'ME' for Month End frequency
    cl_1m = cl_5m.resample('ME').agg(ohlcv_agg)
    # 'QE' for Quarter End frequency (approximates 3 months)
    # Alternatively, use '3ME' for 3-Month End frequency if pandas version supports it
    cl_3m = cl_5m.resample('QE').agg(ohlcv_agg) 

    # --- Clean up potential NaN rows ---
    # Resampling can create rows for periods where there was no original data.
    # It's often good practice to remove these.
    cl_1d.dropna(inplace=True)
    cl_2d.dropna(inplace=True)
    cl_1w.dropna(inplace=True)
    cl_1m.dropna(inplace=True)
    cl_3m.dropna(inplace=True)

    print("\nResampled Daily Data (cl_1d):")
    #print(cl_1d.head())
    
    print("\nResampled Weekly Data (cl_1w):")
    #print(cl_1w.head())

    print("\nResampled Monthly Data (cl_1m):")
    #print(cl_1m.head())
    
def generate_indicators(cl):
    """
    Generate 50 popular technical indicators using pandas_ta.
    """
    # Add indicators to the DataFrame
    #cl['sma_10'] = ta.sma(cl['Close'], length=10)  # Simple Moving Average
    #cl['ema_10'] = ta.ema(cl['Close'], length=10)  # Exponential Moving Average
    cl['rsi'] = ta.rsi(cl['Close'])               # Relative Strength Index

    print("Indicators generated successfully.")
    return cl


def testStrat(verbose=True,plot=False,prices=None,sym='JPM',strat='Manual',daterange=None,window_dict=None,
              impact=0.005,commission =9.95,bags=50,split=0.4, boost=False,epochs=10):
    if verbose == True:
        if strat == 'Manual':
            print("=== Manual Strategy ===")
        elif strat == 'Learner':
            print("===Strategy Learner===")


    sd = daterange.min()
    ed = daterange.max()
    sv = 100000


    #Get trades from manual strategy
    if 'QLearner' in strat:
        QL = sql.StrategyQLearner(verbose=True, epochs = epochs, impact=0.005, commission=9.95)
        QL.add_evidence(symbol=sym, sd=sd, ed=ed)
        trades = QL.testPolicy(symbol=sym, sd=sd, ed=ed)
        trader = QL

    elif 'Learner' in strat:
        SL = sl.StrategyLearner(bags = bags, split = split, verbose = verbose, window_dict=window_dict,impact=impact, commission=commission, boost=boost)
        #Train the SL
        SL.add_evidence(symbol=sym ,sd=sd,ed=ed,sv=sv)
        #Get the trades
        trades = SL.testPolicy(symbol=sym, sd=sd,ed=ed,sv=sv)

        trader = SL 
        #breakpoint()
    
    #get portvals
    manual_vals = msc.compute_portvals(trades,
                                       start_val=100000,
                                       impact = impact,
                                       commission = commission)
    
    #Compare to buy and hold
    #breakpoint()
    benchmark = prices
    #normalize the benchmark al
    benchmark = benchmark/benchmark.iloc[0]
    vals = manual_vals/manual_vals.iloc[0]

    #create buy and sell ticks
    #if manual_trades < 0 then return -1 (sell), otherwise 0, and vice versa, return 1 (buy) otherwise 0.
    buys = np.where(trades.values > 0, benchmark.values, 0)
    sells = np.where(trades.values < 0, benchmark.values, 0)

    #breakpoint()
    if plot == True:
        #create plots to compare
        plt.figure(1, figsize=(12,6))
        plt.title(f"{strat} Strategy vs. Buy and Hold")
        
        #plot values
        plt.axhline(1,linewidth=2,color='black')
        plt.plot(vals, label=f'{strat} Strategy', color='red',linestyle='-',linewidth=2)
        plt.plot(benchmark, label='Buy and Hold', color='purple',linestyle='-', linewidth=1)

        #plot buy/sell ticks
        #plt.xticks(buys,color='black')
        #plt.xticks(manual_vals.index, sells*15, color='blue')
        
        #breakpoint()
        #for date in trades.index:
        fb = 0
        fs = 0
        for i in range(len(trades.index)):
            date = trades.index[i]
            #breakpoint()
            if trades.loc[date].values[0] > 0:
                plt.axvline(x=date,color='black',linestyle='-',linewidth=1, label="Buy" if fb == 0 else "")
                fb = 1
            elif trades.loc[date].values[0] < 0:
                plt.axvline(x=date,color='blue',linestyle='-',linewidth=1, label="Sell" if fs == 0 else "" )
                fs = 1
        

        plt.xlabel('Date')
        plt.ylabel('Normalized Portfolio Value')
        plt.legend()
        #plt.grid(True)

        add_text_watermark(plt.figure(1))
        
        #plt.show()
        plt.savefig(f'{strat}VsBenchmark_{sym}.png')
        plt.close()

    final = manual_vals[-1]
    tot_ret = manual_vals[-1]/manual_vals[0]
    buynhold = prices[-1]/prices[0]
    print(f'Final portfolio value is: {final}\n\nThe total return is: {tot_ret}\nReturn for Buy and Hold: {buynhold}\n')
    return final, tot_ret, trader
    

if __name__ == '__main__':
    print("Starting out now.")
    
    ####Functional testing:
    
    # Define the symbol
    symbol = 'JPM'

    sd = dt.datetime(2020,1,1)
    ed = dt.datetime(2021,1,1)
    dates = pd.date_range(sd,ed)

    prices = ut.get_data([symbol], dates, colname='Close')
    
    auto_portfolio, auto_return, auto_learner = testStrat(prices=prices,verbose=True,plot=True, strat='InLearner',daterange=dates,window_dict=None)
    #retest_strat(auto_learner, sd=sd_test, ed=ed_test, sym='JPM',strat='OutLearner')
    
    #Create CL data
    cl = create_cl_data()
    
    
    
    #Create indicators
    indicators = generate_indicators(cl)
    
    #Build features and labels
    #features = indicators.drop(columns=['Close'])
    #labels = indicators['Close'].shift(-1)  # Predict next day's close
    
    #Train RF
    
    #Obtain trades
    
    #Simulate
    
    #Report metrics and plot
    
    #### Create a wrapper around this loop to change list of indicators being generated then pass the wrapper to an optimizer to grid search for best 
    # parameters for the specific model then report the stats: Model 1: {indicator_list} {params} {performance vs buy and hold}
    
    
    
    
    print("Done.")