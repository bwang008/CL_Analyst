#main helper function to run the project and collect data

import pandas as pd
import numpy as np
#import matplotlib.pyplot as plt
import pandas_ta as ta
import sklearn as sk

#import yfinance as yf

def create_cl_data():
    cols=['Day','Time','Open','High','Low','Close','Volume']
    cl = pd.read_csv('data/test.csv', sep=';', index_col=None, header=None, names=cols)
    #cl = pd.read_csv('data/test10k.csv')
    
    # Assign column names
    
    #cl.columns = cols
    
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
    

if __name__ == '__main__':
    print("Starting out now.")
    
    cl = create_cl_data()