'''
This will serve as a testing shell for the market analyzer and forecaster that I am planning to use for CL trading

There will be a breakdown of the following

-Data builder - Takes the raw csv file for the CL data, and produces the post-processed datasets that will be used for analysis

    -Window analysis - Breaks the raw data into windows which provide summaries of metrics
    -Trade opportunities - Highlights conditions where a buy or a sell would have resulted in significant profit and provides metrics
        for these moments
    -Reversal/Trend finder
'''

import numpy as np
import pandas as pd

#data = pd.read_csv('data/cl-5m_bk.csv',sep=';',parse_dates=[[0,1]],index_col=0,dayfirst=True)
data = pd.read_csv('data/test.csv',sep=';',header=None,index_col=None)
        
cols=['Date','Time','Open','High','Low','Close','Volume']
data.columns = cols

#Merge the date and time columns to form single DT column and assign it as the index/key


breakpoint()
#data.columns(cols)


print(data.head())
