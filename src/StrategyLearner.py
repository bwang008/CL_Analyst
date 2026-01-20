""""""  		  	   		 	 	 			  		 			     			  	 
"""  		  	   		 	 	 			  		 			     			  	 
Template for implementing StrategyLearner  (c) 2016 Tucker Balch  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
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
  		  	   		 	 	 			  		 			     			  	 
Student Name: Tucker Balch (replace with your name)  		  	   		 	 	 			  		 			     			  	 
GT User ID: bwang421 (replace with your User ID)  		  	   		 	 	 			  		 			     			  	 
GT ID: 903470542 (replace with your GT ID)  		  	   		 	 	 			  		 			     			  	 
"""  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
import datetime as dt  		  	   		 	 	 			  		 			     			  	 
import random
import numpy as np
from scipy import stats #This is to calculate the mode (Not sure why it can't be done in numpy..)

import pandas as pd  		  	   		 	 	 			  		 			     			  	 
from . import util as ut  		  	   

#Try to build with RTLearner

from . import LearnerRT as rtl
from . import LearnerBag as bgl
# Note: indicators.py is still in root, keep absolute import
import indicators as ind
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
class StrategyLearner(object):  		  	   		 	 	 			  		 			     			  	 
    """  		  	   		 	 	 			  		 			     			  	 
    A strategy learner that can learn a trading policy using the same indicators used in ManualStrategy.  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
    :param verbose: If “verbose” is True, your code can print out information for debugging.  		  	   		 	 	 			  		 			     			  	 
        If verbose = False your code should not generate ANY output.  		  	   		 	 	 			  		 			     			  	 
    :type verbose: bool  		  	   		 	 	 			  		 			     			  	 
    :param impact: The market impact of each transaction, defaults to 0.0  		  	   		 	 	 			  		 			     			  	 
    :type impact: float  		  	   		 	 	 			  		 			     			  	 
    :param commission: The commission amount charged, defaults to 0.0  		  	   		 	 	 			  		 			     			  	 
    :type commission: float  		  	   		 	 	 			  		 			     			  	 
    """  		  	   		 	 	 			  		 			     			  	 
    # constructor
    def author(self):
        return 'bwang421'

    def study_group(self):
        return 'bwang421'

    def gtid(self):
        return '903470542'  
    		  	   		 	 	 			  		 			     			  	 
    def __init__(self, verbose=False, 
                 impact=0.005, 
                 commission=9.95, 
                 learner=rtl.RTLearner,
                 window_dict = None,
                 bags = 50,
                 boost = False,
                 split = 0.4):
        """  		  	   		 	 	 			  		 			     			  	 
        Constructor method  		  	   		 	 	 			  		 			     			  	 
        """  		  	   		 	 	 			  		 			     			  	 
        self.verbose = verbose  		  	   		 	 	 			  		 			     			  	 
        self.impact = impact  		  	   		 	 	 			  		 			     			  	 
        self.commission = commission
        self.learner = learner #Add learner for training ensemble
        if window_dict == None:
            self.window_dict = {'default':20,
                    'rsi':20, #short - [9,10] long - [20,25]
                    'sma':8, #short - [5,10,20] long - [50,200]
                    'momentum':20, # 10,12, 20. 
                    'stochastic':14, #k 3, d 14,
                    'bollinger':10 #10, 20, 50
                    } #maybe set this to a dictionary in order to handle variance in indicators
        else:
            self.window_dict = window_dict

        #data = [rsi.values, sma.values, mm.values, stoc.values, bb.values, volume.values, prices.index.month]
        #calculation for thresh, we want the value of commission and impact to slowly make thresh so high it becomes untenible
        #0.03 (baseline + impact + (commission/1000))
        self.thresh = 0.022 + self.impact + self.commission/1000 #pct change to register for training data
        
        if boost == False:
            self.thresh_window = 7  #number of days to consider for the pct change
        else:
            self.thresh_window = 7

        self.tree = None
        self.leaf_size = 6
        #baglearner params
        self.bags = bags
        self.boost = boost
        self.split = split #split to train different bag learners on.

        self.RTL = None #placeholder for testing.

        #self.learner = rtl.RTLearner(verbose=False)
        #self.learner = dtl.DTLearner(verbose=False)
        # 
        # #verbose=False, learner=None, bags=10, boost=False, kwargs={'leaf_size':1},split=0.6):
        kwargs = {'leaf_size':self.leaf_size}
        self.baglearner = bgl.BagLearner(verbose=verbose, bags = self.bags, learner = self.learner, boost=self.boost, split=self.split, kwargs=kwargs)
  		  	   		 	 	 			  		 			     			  	 
    
    def add_evidence(self,symbol="JPM",sd=dt.datetime(2008, 1, 1),ed=dt.datetime(2009, 12, 31),sv=10000):
        # example usage of the old backward compatible util function
        syms = [symbol]
        dates = pd.date_range(sd, ed)
        prices_all = ut.get_data(syms, dates, colname="Close")  # automatically adds SPY
        if self.verbose:
            print(f"DEBUG: prices_all shape: {prices_all.shape}")
            print(f"DEBUG: prices_all columns: {prices_all.columns}")
            print(f"DEBUG: syms: {syms}")
            print(f"DEBUG: symbol: {symbol}")
            print(f"DEBUG: prices_all.dtypes: {prices_all.dtypes}")
            print(f"DEBUG: syms type: {type(syms)}")
            print(f"DEBUG: syms[0] type: {type(syms[0])}")
            print(f"DEBUG: prices_all.columns.dtype: {prices_all.columns.dtype}")
        try:
            prices = prices_all[syms]  # only portfolio symbols
            if self.verbose:
                print(f"DEBUG: Successfully accessed prices_all[syms]")
                print(f"DEBUG: prices shape: {prices.shape}")
        except Exception as e:
            if self.verbose:
                print(f"DEBUG: Error accessing prices_all[syms]: {e}")
                print(f"DEBUG: Trying alternative access method...")
            # Try alternative access method
            prices = prices_all.loc[:, syms]
        #prices_SPY = prices_all["SPY"]  # only SPY, for comparison later  		  	   		 	 	 			  		 			     			  	 
        #if self.verbose:  		  	   		 	 	 			  		 			     			  	 
        #    print(prices)  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
                # example use with new colname
        if self.verbose:
            print(f"DEBUG: Getting volume data...")
        volume_all = ut.get_data(
            syms, dates, colname="Volume"
        )  # automatically adds SPY
        if self.verbose:
            print(f"DEBUG: volume_all shape: {volume_all.shape}")
            print(f"DEBUG: volume_all columns: {volume_all.columns}")
        try:
            volume = volume_all[syms]  # only portfolio symbols
            if self.verbose:
                print(f"DEBUG: Successfully accessed volume_all[syms]")
        except Exception as e:
            if self.verbose:
                print(f"DEBUG: Error accessing volume_all[syms]: {e}")
            # Try alternative access method
            volume = volume_all.loc[:, syms]
        #volume_SPY = volume_all["SPY"]  # only SPY, for comparison later
        #if self.verbose:
        #    print(volume)

        #Now that we have the indicators, we need to create training data (maybe move to another func later)
        #take the current day's indicators and then the P/L from the next day as the Y as -1,0,1

        window_dict = self.window_dict

        rsi = ind.rsi(prices,window_dict['rsi'])
        sma = ind.moving_average(prices, window_dict['sma'])
        mm = ind.momentum(prices, window_dict['momentum'])
        stoc = ind.stochastic_oscillator(prices, window_dict['stochastic'])
        bb = ind.bollinger_bands(prices, window_dict['bollinger'])

        #Change the shift value to make it more pronounced/fewer trades

        #Different strategies for finding training windows
        #daily_ret = prices.pct_change().shift(self.thresh_window) #This works OK, but it isn't really intuitive
        #It is taking the daily return since pct_change is default to 1


        #daily_ret = prices.pct_change(self.thresh_window).shift(-self.thresh_window) 

        #shift index back meaning for index
        returns = prices.pct_change(self.thresh_window).shift(self.thresh_window)
        '''
        For some reason, shift() by positive window nets better results. This means that the loss from the previous (window)
        days is lined up with the current day vs future return being the target to train the data on 
        i.e. if 5 days ago, the delta between the price and today is -5 pct meaning price has gone down,
        positive shift lines up the indicators for today with the negative -5 of the last 5 days (assuming 5 window size)
        rather than 5 days earlier...

        Intuitively, I would think training on the future profit/loss would be smarter to try to forecast future movement 
        but the results are bad actually, and doing this positive shift nets positive P/L more than any other tuning I've done.

        When I chose to train on past data, the results suddenly became positive and consistently for both in and out sample data

        I don't fully understand why it is the case but I don't think it is an accident. 
        '''


        #breakpoint()
        target = pd.DataFrame(index=returns.index, columns=['y'])
        #if the pct change is higher than threshold, then update.
        #1 - up day
        #-1 - down day
        #0 - not worth trading

        thresh = self.thresh
        target.loc[returns[symbol].values > thresh, 'y'] = 1
        target.loc[returns[symbol].values < -thresh, 'y'] = -1
        target.loc[(returns[symbol].values <= thresh) & (returns[symbol].values >= -thresh), 'y'] = 0

        #breakpoint()
        #data = [rsi.values, sma.values, mm.values, stoc.values, bb.values, volume.values, prices.index.month, target.values]
        #data = [rsi.values, sma.values, mm.values, bb.values, target.values]
        #breakpoint()
        data = [rsi.values,  mm.values, bb.values, target.values]

        data = np.column_stack(data)
        
        data = np.array(pd.to_numeric(data.flatten(), errors='coerce')).reshape(data.shape)

        nan_mask = np.isnan(data).any(axis=1)
        data = data[~nan_mask] #remove all nan rows so window_dict at beginning and the last row of y for returns

        x_data = data[:,:-1]
        y_data = data[:,-1] #get the last column

        trade_actions = y_data[np.isin(y_data,[-1,1])]
        self.num_data = [trade_actions.shape[0],data.shape[0]] #number of training instances for buy and sell actions

        if self.verbose == True:
            print(f"Found {data.shape[0]} training windows, {trade_actions.shape[0]} actions, with parameters:\n \
                Profit Thresh: {self.thresh} Commission: {self.commission} Impact: {self.impact}\n \
                Threshold window size: {self.thresh_window}\n \
                Indicator windows: {self.window_dict}\n \
                Bags: {self.bags} LeafSize: {self.leaf_size} Split: {self.split}")

        #breakpoint()
        #now we have x,y data, now we need to train the RT on it (use one RT then ensemble after)

        ####Training
        #Build ensemble
        
        self.tree = self.baglearner.add_evidence(x_data,y_data)
        
        #self.RTL = self.learner()
        #self.tree = self.RTL.add_evidence(x_data,y_data)

        #breakpoint()

        return self.tree

    def find_trades(self):
        #helper function to build training data for the DT learner
        test = 1

  		  	   		 	 	 			  		 			     			  	 
    # this method should use the existing policy and test it against new data  		  	   		 	 	 			  		 			     			  	 
    def testPolicy(  		  	   		 	 	 			  		 			     			  	 
        self,  		  	   		 	 	 			  		 			     			  	 
        symbol="JPM",
        sd=dt.datetime(2008, 1, 1),
        ed=dt.datetime(2009, 12, 31),
        sv=10000,  		  	   		 	 	 			  		 			     			  	 
    ):
  	   		 	 	 			  		 			     			  	 
        syms = [symbol]  		  	   		 	 	 			  		 			     			  	 
        dates = pd.date_range(sd, ed)  		  	   		 	 	 			  		 			     			  	 
        prices_all = ut.get_data(syms, dates, colname="Close")  # automatically adds SPY  		  	   		 	 	 			  		 			     			  	 
        prices = prices_all[syms]  # only portfolio symbols  		  	   		 	 	 			  		 			     			  	 	  	   		 	 	 			  		 			     			  	 	  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
        # example use with new colname  		  	   		 	 	 			  		 			     			  	 
        volume_all = ut.get_data(  		  	   		 	 	 			  		 			     			  	 
            syms, dates, colname="Volume"  		  	   		 	 	 			  		 			     			  	 
        )  # automatically adds SPY
        volume = volume_all[syms]  # only portfolio symbols  		  	   		 	 	 			  		 			     			  	 	  	   		 	 	 			  		 			     			  	 


        #Now that we have the indicators, we need to create training data (maybe move to another func later)
        #take the current day's indicators and then the P/L from the next day as the Y as -1,0,1

        window_dict = self.window_dict

        rsi = ind.rsi(prices,window_dict['rsi'])
        sma = ind.moving_average(prices, window_dict['sma'])
        mm = ind.momentum(prices, window_dict['momentum'])
        stoc = ind.stochastic_oscillator(prices, window_dict['stochastic'])
        bb = ind.bollinger_bands(prices, window_dict['bollinger'])

        #data = [rsi.values, sma.values, mm.values, stoc.values, bb.values, volume.values, prices.index.month]
        #data = [rsi.values, sma.values, mm.values, bb.values]

        data = [rsi.values, mm.values, bb.values]
        data = np.column_stack(data)
        
        data = np.array(pd.to_numeric(data.flatten(), errors='coerce')).reshape(data.shape)

        nan_mask = np.isnan(data).any(axis=1)
        data = data[~nan_mask] #remove all nan rows so window_dict at beginning and the last row of y for returns

        x_data = data
        #y_data = data[:,-1] #get the last column

        y_pred = self.baglearner.query(x_data)
        #y_pred = self.RTL.query(x_data)

        df_trades = pd.DataFrame(index=prices.index,columns=['Shares'])
        df_trades['Shares'] = 0
        #breakpoint()
        #Assuming y_pred is finished and we get the predictive -1,0,1 for sell,hold,buy then we can build the df_trades 
        num_trades = 0
        position = 0
        for idx,value in enumerate(y_pred):
            if value == 1 and position != 1000: #buy
                if position == -1000: #Already short
                    df_trades.iloc[idx]['Shares'] = 2000
                else:
                    df_trades.iloc[idx]['Shares'] = 1000
                position = 1000
                num_trades+=1
            elif value == -1 and position != -1000: #sell
                if position == 1000:
                    df_trades.iloc[idx]['Shares'] = -2000
                else:
                    df_trades.iloc[idx]['Shares'] = -1000
                position = -1000
                num_trades+=1
        #breakpoint()
        self.num_trades = num_trades
        return df_trades
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
if __name__ == "__main__":  		  	   		 	 	 			  		 			     			  	 
    print("One does not simply think up a strategy")  		  

    np.set_printoptions(suppress=True)		     			  	 
    sl = StrategyLearner()
    #sl.testPolicy()
    sl.add_evidence() #Call this to create self.tree 
    trades = sl.testPolicy() #Call this to run against self.tree and get back df_trades
    print("Some of the trades:\n",trades.loc[trades.Shares != 0])