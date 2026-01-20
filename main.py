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
import src.util as util
import src.indicatorBuilder as ind

#data = pd.read_csv('data/cl-5m_bk.csv',sep=';',parse_dates=[[0,1]],index_col=0,dayfirst=True)
# Commented out - this code runs on import and causes issues when importing from notebooks
# If needed, move this inside the if __name__ == '__main__' block or use absolute paths
# data = pd.read_csv('data/test.csv',sep=';',header=None,index_col=None)
# cols=['Date','Time','Open','High','Low','Close','Volume']
# data.columns = cols

#Merge the date and time columns to form single DT column and assign it as the index/key


#breakpoint()
#data.columns(cols)


#data.head()

def get_cl_df(cl_test_data="data/test.csv"):
    #Get the data
    cl_test_data = util.get_cl_data(cl_test_data)
    features = ind.generate_features(cl_test_data)
    #print(features.head())
    return features

if __name__ == '__main__':
    features = get_cl_df()
    print(features.head())
    

    