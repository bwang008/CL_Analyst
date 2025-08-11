# StrategyQLearner.py

import datetime as dt
import random
import numpy as np
import pandas as pd
import util as ut
import indicators as ind
import QLearner as ql
import pdb # For debugging if needed

class StrategyQLearner(object):
    """
    A strategy learner that uses Q-Learning to learn a trading policy.
    It uses the same indicators as ManualStrategy and StrategyLearner.

    :param verbose: If “verbose” is True, your code can print out information for debugging.
    :type verbose: bool
    :param impact: The market impact of each transaction, defaults to 0.005
    :type impact: float
    :param commission: The commission amount charged, defaults to 9.95
    :type commission: float
    :param num_bins: The number of bins to discretize each indicator into.
    :type num_bins: int
    :param epochs: The number of times to iterate over the training data.
    :type epochs: int
    :param alpha: The learning rate for the Q-Learner.
    :type alpha: float
    :param gamma: The discount factor for the Q-Learner.
    :type gamma: float
    :param rar: Random action rate for the Q-Learner.
    :type rar: float
    :param radr: Random action decay rate for the Q-Learner.
    :type radr: float
    :param dyna: Number of Dyna-Q updates per step.
    :type dyna: int
    """

    def author(self):
        return 'bwang421' # Replace with your GT User ID

    def gtid(self):
        return '903470542' # Replace with your GT ID

    def study_group(self):
        return 'bwang421' # Replace with your study group if applicable

    def __init__(self, verbose=False, impact=0.005, commission=9.95,
                 num_bins=7, epochs=10, alpha=0.2, gamma=0.9,
                 rar=0.6, radr=0.9995, dyna=0):
        """
        Constructor method
        """
        self.verbose = verbose
        self.impact = impact
        self.commission = commission
        self.epochs = epochs
        self.num_bins = num_bins
        self.learner = None
        self.alpha = alpha
        self.gamma = gamma
        self.rar = rar
        self.radr = radr
        self.dyna = dyna
        self.num_actions = 3 # Corresponds to SELL, HOLD, BUY
        self.num_states = 0 # Will be calculated based on indicators and bins
        self.indicator_bin_edges = {} # To store bin edges for discretization
        self.indicators = ['rsi', 'momentum', 'bb_percent'] # Select indicators
        
        self.state_history = {} #dictionary to keep track of the frequency that we see different states to get an idea of how sparse the state map is for training.
        # Default window sizes (can be overridden if needed, but keep simple for QL)
        self.window_dict = {
            'rsi': 14,
            'momentum': 14,
            'bollinger': 20  # Window for Bollinger Bands calculation
        }
        # Derived indicator names used for column access
        self.indicator_cols = ['rsi', 'momentum', 'bb_percent']

        self.holdings = 0 #the amount of stock we are assumed to be holding, or leverage indicator.
        self.threshold = 0.015 #Threshold for reward calculation.


    def _calculate_indicators(self, prices):
        """
        Calculates the selected technical indicators.

        :param prices: DataFrame of prices for a single symbol.
        :type prices: pd.DataFrame
        :return: DataFrame with indicator values.
        :rtype: pd.DataFrame
        """
        indicator_df = pd.DataFrame(index=prices.index)
        # Ensure prices is a DataFrame with one column for the symbol
        if not isinstance(prices, pd.DataFrame) or prices.shape[1] != 1:
             raise ValueError("Input 'prices' must be a DataFrame with a single symbol column.")
        symbol = prices.columns[0]
        price_series = prices[symbol] # Use the Series for calculations

        # RSI - indicator function expects a DataFrame but works with Series too based on .diff()
        # It returns a DataFrame, so select the column
        rsi_val_df = ind.rsi(prices, window=self.window_dict['rsi'])
        indicator_df['rsi'] = rsi_val_df[symbol]

        # Momentum - indicator function expects a DataFrame but works with Series too based on .shift()
        # It returns a DataFrame, so select the column
        momentum_val_df = ind.momentum(prices, window=self.window_dict['momentum'])
        indicator_df['momentum'] = momentum_val_df[symbol]

        # Bollinger Bands %B (Percent B)
        # CORRECTED: Call the existing bollinger_bands function which returns %B directly.
        # Pass the price *Series* to the indicator function.
        bb_percent_series = ind.bollinger_bands(price_series, window=self.window_dict['bollinger'])
        indicator_df['bb_percent'] = bb_percent_series # Assign the returned Series

        # Add other indicators here if needed, update self.indicators and self.indicator_cols
        # Example: SMA ratio
        # sma_short_df = ind.moving_average(prices, 10)
        # sma_long_df = ind.moving_average(prices, 50)
        # indicator_df['sma_ratio'] = sma_short_df[symbol] / sma_long_df[symbol]
        # self.indicators.append('sma_ratio') # Update list if adding
        # self.indicator_cols.append('sma_ratio') # Update list if adding

        return indicator_df


    def _discretize(self, indicator_df):
        """
        Calculates bin edges for each indicator based on training data quantiles.
        Stores the edges in self.indicator_bin_edges.

        :param indicator_df: DataFrame of indicator values (training data).
        :type indicator_df: pd.DataFrame
        """
        if self.verbose: print("Calculating discretization bins...")
        for indicator in self.indicator_cols:
            # Use quantiles to define bins - robust to outliers
            # Drop NaNs before calculating quantiles
            clean_data = indicator_df[indicator].dropna()
            if clean_data.empty:
                # Handle cases where an indicator might be all NaN (unlikely with proper windowing)
                # Fallback to arbitrary linear bins if needed, or raise error
                print(f"Warning: Indicator {indicator} has no valid data for binning. Using default range.")
                # Example fallback: use min/max if available, else fixed range
                min_val, max_val = indicator_df[indicator].min(), indicator_df[indicator].max()
                if pd.isna(min_val) or pd.isna(max_val) or min_val == max_val:
                         min_val, max_val = -1, 1 # Arbitrary fallback
                self.indicator_bin_edges[indicator] = np.linspace(min_val, max_val, self.num_bins + 1)

            else:
                # Calculate bin edges using quantiles
                # Adding endpoints to ensure all values are covered
                quantiles = np.linspace(0, 1, self.num_bins + 1)
                edges = clean_data.quantile(quantiles).values
                # Ensure edges are unique, handle potential duplicates from quantiles
                edges = np.unique(edges)
                if len(edges) < 2: # Not enough unique values to create bins
                    # Fallback: use min/max or a small range around the single value
                        min_val, max_val = clean_data.min(), clean_data.max()
                        if min_val == max_val:
                            min_val = min_val - 0.01 # Create a small range
                            max_val = max_val + 0.01
                        edges = np.linspace(min_val, max_val, self.num_bins + 1)
                elif len(edges) < self.num_bins + 1:
                    # If fewer unique edges than needed, pad with linear spacing at ends
                        edges = np.linspace(edges[0], edges[-1], self.num_bins + 1)

                # Add -inf and +inf to catch outliers outside the quantile range
                edges[0] = -np.inf
                edges[-1] = np.inf
                self.indicator_bin_edges[indicator] = edges

            if self.verbose:
                    print(f"   Indicator '{indicator}' bins: {self.indicator_bin_edges[indicator]}")


    def _get_state(self, indicator_values):
        """
        Maps a set of indicator values to a discrete state index.

        :param indicator_values: A list or array of indicator values for a single timestep.
                                 Order must match self.indicator_cols.
        :type indicator_values: np.array or list
        :return: The calculated state index. Returns -1 if any value is NaN or cannot be binned.
        :rtype: int
        """
        state = 0
        base = 1
        for i, indicator in enumerate(self.indicator_cols):
            value = indicator_values[i]
            if pd.isna(value) or indicator not in self.indicator_bin_edges:
                print(f"  Warning: NaN or missing bins for {indicator} with value {value}") # Debugging line
                return -1 # Invalid state if NaN or bins not defined

            # Find the bin index for the current indicator value
            # np.digitize returns index i where edges[i-1] <= value < edges[i]
            # Bins are 0-indexed, so subtract 1
            # Use right=False so the interval is [left, right)
            bin_index = np.digitize(value, self.indicator_bin_edges[indicator], right=False) - 1

            # Ensure bin_index is within valid range [0, num_bins-1]
            # np.digitize might return 0 (if value < edges[0]) or len(edges) (if value >= edges[-1])
            # Since we set edges[0] = -inf and edges[-1] = inf, we expect indices 1 to num_bins.
            # Subtracting 1 gives 0 to num_bins-1. Clamping handles edge cases.
            bin_index = max(0, min(bin_index, self.num_bins - 1))

            # Combine bin indices to form the state number 
            state += bin_index * base
            base *= self.num_bins # Increment base for the next indicator's contribution

        return state

    def _calculate_reward(self,action, price, rets, case=1):
         #rets = future returns. Trade today, get rewarded with result from tomorrow. Maybe we make this a weighted sum in the future.
         #Initial thinking is today + tomorrow*0.5 + day after*0.25 but maybe backwards looking will also be good to experiment with.
            
                

            #I think that a dedicated reward function is a good idea to experiment with

            #The idea is that we will precompute each day and the reward for each action i.e.

            #If today is monday, I want to calculate the reward for monday based on the following scenarios

            #If I am long, and tomorrow is going up, then give a  positive reward, but also look ahead 5 days and add the difference as a return with a weight
                #Therefore the acceptable actions here are to say go long, or hold if I am long and the day is positive. 
                #If I am short, the acceptable answer here is long. Hold and short will both return a negative penalty with the same return. 

                #If we want to make holding a more popular action to prevent overtrading there's 2 things to try:
                #1. implement commission and impact for the reward function
                #2. increase the reward for holding and increase the penalty for losing money, and reduce the reward for making immediate money. 
                #Increase the reward long term rather than making immediate higher weight to see if that makes it behave any differently.

                #Case 1 - simple reward calc
                #Case 2 - precompute the reward for each day and return the specified reward for that day from the precomputed action. 
                #This should give more flexiblity in modifying the reward and easily testing by setting aside different cases rather than modifying the same location.
         if case == 1:
            reward = rets * self.holdings


            if action != 1:
                stophere =1
            else:
                stophere=1
            value = 0

            if action == 0: #sell
                if self.holdings != -1000:
                    shares = -1000 - self.holdings
                    value = (shares*price*(rets+self.impact))-self.commission
                else: #No trade, maintain position.
                    shares = -1000
                    value = shares*price*rets
                self.holdings = -1000
                
            elif action == 2: #buy
                if self.holdings != 1000:
                    shares = 1000 - self.holdings
                    value = (shares*price*(rets-self.impact))-self.commission
                else:
                    shares = 1000
                    value = shares*price*rets
                self.holdings = 1000
                
            elif action == 1: #hold
                shares = self.holdings
                value = self.holdings*price*rets
            else:
                raise IndexError("Invalid Value for Action")
            if shares == None:
                stophere=1
            
            value_per_share = value/np.abs(shares)
            percent_change = value_per_share/price
            
            #Tried to use thresholding reward, not doing that great. Let's go back to percentage as reward.
            # if percent_change > self.threshold:
            #     reward = 1
            # elif percent_change < -self.threshold:
            #     reward = -2
            # elif percent_change < np.abs(self.threshold):
            #     reward = 0.1
            
            reward = reward # Try to change this later
            return reward
         elif case == 2:
             #Try to precompute the rewards here to save time, and also make it easier to modify the logic in the future.
            donothing = 1
        
        

              

         



    def add_evidence(self, symbol="JPM", sd=dt.datetime(2008, 1, 1),
                     ed=dt.datetime(2009, 12, 31), sv=10000):
        """
        Trains the Q-Learner policy by iterating over historical data.

        :param symbol: The stock symbol to train on.
        :type symbol: str
        :param sd: Start date for training data.
        :type sd: datetime
        :param ed: End date for training data.
        :type ed: datetime
        :param sv: Start value of the portfolio (not directly used in QL training but standard).
        :type sv: int
        """
        # Get price data
        syms = [symbol]
        dates = pd.date_range(sd, ed)
        prices_all = ut.get_data(syms, dates) # Get data including SPY
        prices = prices_all[syms] # Isolate the symbol's prices (DataFrame)
        # prices_SPY = prices_all["SPY"] # Keep SPY for reference if needed, but drop

        # Ensure prices for the specific symbol are forward-filled then back-filled
        prices = prices.ffill().bfill()

        if prices.empty or prices.isnull().all().all():
            print(f"Error: No valid price data found for {symbol} in the given date range.")
            return

        # Calculate indicators using the corrected function
        indicator_df = self._calculate_indicators(prices)

        # Combine prices and indicators, handle NaNs by dropping initial rows
        data_df = indicator_df.join(prices, how='inner')
        # Calculate initial NaNs based on the *largest* window size used + 1 (for diff/shift)
        max_window = max(self.window_dict.values())
        # Drop initial rows where indicators would be NaN
        # data_df = data_df.iloc[max_window:] # Alternative drop based on max window
        data_df = data_df.dropna() # Drop any row with NaN in indicators or price

        if data_df.empty:
            print("Error: Not enough data after calculating indicators and dropping NaNs.")
            return

        # Discretize states based on the *training* data
        self._discretize(data_df[self.indicator_cols])
        self.num_states = self.num_bins ** len(self.indicator_cols)

        if self.verbose:
             print(f"Number of indicators: {len(self.indicator_cols)}")
             print(f"Bins per indicator: {self.num_bins}")
             print(f"Total states: {self.num_states}")
             print(f"Training epochs: {self.epochs}")
             print(f"Training data range: {data_df.index.min()} to {data_df.index.max()}")
             print(f"Training data shape: {data_df.shape}")

        # Initialize the Q-Learner
        self.learner = ql.QLearner(num_states=self.num_states,
                                   num_actions=self.num_actions,
                                   alpha=self.alpha,
                                   gamma=self.gamma,
                                   rar=self.rar,
                                   radr=self.radr,
                                   dyna=self.dyna,
                                   verbose=True)
        
        if self.verbose:
            print(f"=PARAMETERS=\n radr: {self.radr} alpha:{self.alpha} gamma:{self.gamma}\n")

        # --- Q-Learning Training Loop ---
        lastreward = None
        for epoch in range(1, self.epochs + 1):
            if self.verbose == True:
                 print(f"   Starting Epoch: {epoch}")
            if self.verbose and epoch % 5 == 0: print(f"Epoch {epoch}/{self.epochs}")

            total_reward_epoch = 0
            current_holdings = 0 # Start with no position (0 shares) represented by 0 state

            # Get initial state from the first valid day in data_df
            initial_indicators = data_df.iloc[0][self.indicator_cols].values
            s = self._get_state(initial_indicators)
            if s == -1:
                print(f"Warning: Could not determine initial state for epoch {epoch}. Skipping epoch.")
                # Find first valid state if the very first row fails
                first_valid_index = -1
                for idx in range(data_df.shape[0]):
                     s_try = self._get_state(data_df.iloc[idx][self.indicator_cols].values)
                     if s_try != -1:
                         s = s_try
                         first_valid_index = idx
                         break
                if first_valid_index == -1:
                     print("Error: No valid states found in the training data.")
                     return # Cannot train
                start_index = first_valid_index + 1 # Start iteration from the next day
                if self.verbose: print(f"Starting from index {start_index} due to initial invalid state.")
            else:
                start_index = 5 # Start from the second day as usual

            action = self.learner.querysetstate(s) # Initial action based on first state

            # Iterate through the data day by day, starting from the second valid day
            #Here we want to precompute the rewards for each day and the actions

            for i in range(start_index, data_df.shape[0]-1):
                # Get current indicator values and price for state s_prime
                current_indicators = data_df.iloc[i][self.indicator_cols].values
                s_prime = self._get_state(current_indicators)
                #if self.verbose == True and i % 100 == 0:
                #     print(f"  s_prime: {s_prime}")

                # Get price info for reward calculation
                current_price = data_df.iloc[i][symbol]
                prev_price = data_df.iloc[i - 1][symbol]
                next_price = data_df.iloc[i+1][symbol]

                # Calculate reward based on *holding resulting from previous action* and price change
                daily_return = (current_price / prev_price) - 1
                future_return = (next_price / current_price) - 1
                reward = 0

                reward = self._calculate_reward(action=action, price=current_price, rets=future_return, case=1)
                
                if s_prime == -1:
                    # If next state is invalid, cannot query learner.
                    # Option 1: Give 0 reward and stop episode?
                    # Option 2: Use last valid state/action?
                    # Option 3: Skip update?
                    if self.verbose: print(f"Warning: Invalid next state s_prime at index {i}. Using previous action.")
                    # Keep the previous action, update state s to the invalid one (-1), reward is calculated above.
                    # The learner won't be updated for this transition.
                    # We need an action for the *next* step, let's reuse the current 'action'.
                    pass # Action remains the same as decided in the last valid state
                else:
                     # Update Q-table and get the next action for state s_prime
                     action = self.learner.query(s_prime, reward)
                     #print(f"New action: {action}")
                # Update state for the next iteration
                s = s_prime
                total_reward_epoch += reward


            if self.verbose and epoch % 5 == 0:
                print(f"   Epoch {epoch} total reward: {total_reward_epoch}")
                
            if lastreward != None:
                converge_delta = np.abs((total_reward_epoch-lastreward)/lastreward)
                if converge_delta < 0.01:
                    print(f"Learning rate converged.\nBefore:{lastreward} After:{total_reward_epoch}")
                    break
                lastreward=total_reward_epoch
            else:
                lastreward=total_reward_epoch

        if self.verbose: print("\nTraining complete.")


    def testPolicy(self, symbol="JPM", sd=dt.datetime(2010, 1, 1),
                   ed=dt.datetime(2011, 12, 31), sv=10000):
        """
        Tests the learned Q-Learning policy on new data.

        :param symbol: The stock symbol to test on.
        :type symbol: str
        :param sd: Start date for testing data.
        :type sd: datetime
        :param ed: End date for testing data.
        :type ed: datetime
        :param sv: Start value of the portfolio (used for context, not direct calculation here).
        :type sv: int
        :return: DataFrame with trading orders (1000, -1000, 0).
        :rtype: pd.DataFrame
        """
        if self.learner is None or not self.indicator_bin_edges:
            print("Error: Learner has not been trained or discretization bins are missing. Call add_evidence first.")
            # Return empty DataFrame matching expected format
            dates = pd.date_range(sd, ed)
            prices_all_empty = ut.get_data([], dates) # Get trades index aligned with trading days
            return pd.DataFrame(0, index=prices_all_empty.index, columns=['Shares'])


        # Get price data for testing period
        syms = [symbol]
        dates = pd.date_range(sd, ed)
        prices_all = ut.get_data(syms, dates)
        prices = prices_all[syms]
        #prices = prices.ffill().bfill() # Fill NaNs for robustness

        if prices.empty or prices.isnull().all().all():
            print(f"Error: No price data found for {symbol} during the testing period.")
            return pd.DataFrame(0, index=prices_all.index, columns=['Shares']) # Use prices_all index

        # Calculate indicators for the test period
        indicator_df = self._calculate_indicators(prices)

        # Combine and align data
        data_df = indicator_df.join(prices, how='inner')
        #Forward fill and backfill to get rid Nans still left from indicator calculations
        #if data_df.isnull().any().sum() > 0:
        #     print("Warning: Nan values are being forward and backfilled in the data. Might want to investigate the cause and the degree this is impacting the data.")
        data_df[self.indicator_cols] = data_df[self.indicator_cols].ffill().bfill()

        # --- Testing Loop ---
        # Use the index from the original prices_all to ensure trades align with market days
        df_trades = pd.DataFrame(0, index=prices_all.index, columns=['Shares'])
        # Align data_df to the same index for lookup
        data_df = data_df.reindex(prices_all.index).ffill() # ffill again after reindex

        current_holdings = 0 # Start with 0 shares
        target_holdings = 0

        for i in range(data_df.shape[0]):
            current_date = data_df.index[i]
            # Get current state using the *learned* bin edges
            current_indicators = data_df.iloc[i][self.indicator_cols].values
            s = self._get_state(current_indicators)

            if s == -1:
                 # If state is invalid (e.g., start of data before indicators are ready after ffill/bfill failed)
                 # Default to taking no action (maintain previous holding implied by no trade)
                 action = 1 # Hold equivalent (implies moving towards 0 if not already there)
                 if self.verbose: print(f"Warning: Invalid state at {current_date}. Assuming HOLD action.")
            else:
                 # Query the learned policy (no exploration, rar=0)
                 # Get the action with the highest Q-value for the current state
                 action = np.argmax(self.learner.q_table[s])

                 #investigate here why the QLearner is not always returning hold when the reward for hold is 10:
                #  if action != 1:
                #       debug=True

            # Determine trade based on action and current holdings to reach target state
            trade_shares = 0
            #target_holdings = 0
            if action == 0: target_holdings = -1000 # Sell signal -> target short
            elif action == 2: target_holdings = 1000 # Buy signal -> target long
            # Else action is 1 (Hold signal) -> target cash (0)
            elif action == 1: target_holdings = current_holdings # Do nothing and maintain current position

            trade_shares = target_holdings - current_holdings
            current_holdings = target_holdings # Update holdings for the next day

            if trade_shares != 0:
                 # Ensure the date exists in df_trades index (it should due to reindex)
                 if current_date in df_trades.index:
                     df_trades.loc[current_date, 'Shares'] = trade_shares
                 # else: # Should not happen with reindex
                 #     if self.verbose: print(f"Warning: Date {current_date} not in trades index.")


        if self.verbose:
            print("Testing complete. Trades generated.")
            # print(df_trades[df_trades['Shares'] != 0])

        # Return the trades DataFrame, already indexed correctly
        return df_trades


if __name__ == "__main__":
    print("Strategy QLearner - Example Run")

    # Example usage:
    ql_learner = StrategyQLearner(verbose=True, impact=0.005, commission=0.0, # Set commission=0 for simpler reward/testing
                                  num_bins=10, epochs=50, alpha=0.2, gamma=0.9,
                                  rar=0.5, radr=0.99, dyna=0)

    # Train the learner
    print("\n--- Training ---")
    ql_learner.add_evidence(symbol="JPM",
                            sd=dt.datetime(2008, 1, 1),
                            ed=dt.datetime(2009, 12, 31),
                            sv=100000)

    # Test the learner
    print("\n--- Testing ---")
    trades_df = ql_learner.testPolicy(symbol="JPM",
                                      sd=dt.datetime(2010, 1, 1),
                                      ed=dt.datetime(2011, 12, 31),
                                      sv=100000)

    print("\n--- Sample Trades ---")
    print(trades_df[trades_df['Shares'] != 0].head(10))
    print(f"Total trades generated: {len(trades_df[trades_df['Shares'] != 0])}")

    # Optional: Run through marketsim
    try:
        import importlib
        msc_module = importlib.import_module("marketsimcode") # Use importlib
        compute_portvals = getattr(msc_module, "compute_portvals") # Get function

        portvals = compute_portvals(trades_df, start_val=100000, commission=0.0, impact=0.005) # Match commission/impact
        # Ensure portvals is aligned with trades_df for comparison
        portvals = portvals.reindex(trades_df.index, method='ffill')

        print("\n--- Portfolio Value Stats ---")
        print(f"Start Date: {portvals.index.min()}")
        print(f"End Date: {portvals.index.max()}")
        print(f"Start Value: {portvals.iloc[0]}")
        print(f"End Value: {portvals.iloc[-1]}")

        # Calculate Cumulative Return
        cumulative_return = (portvals.iloc[-1] / portvals.iloc[0]) - 1
        print(f"Cumulative Return: {cumulative_return:.6f}")

        # Calculate Daily Returns
        daily_returns = (portvals / portvals.shift(1)) - 1
        daily_returns = daily_returns.iloc[1:] # Remove first NaN row

        # Calculate Sharpe Ratio (assuming risk-free rate of 0)
        avg_daily_return = daily_returns.mean()
        std_daily_return = daily_returns.std()
        sharpe_ratio = (avg_daily_return / std_daily_return) * np.sqrt(252) # Annualized
        print(f"Average Daily Return: {avg_daily_return:.6f}")
        print(f"Std Dev Daily Return: {std_daily_return:.6f}")
        print(f"Annualized Sharpe Ratio: {sharpe_ratio:.6f}")

    except ImportError:
         print("\nCould not import marketsimcode. Run marketsim manually.")
    except AttributeError:
         print("\nCould not find compute_portvals in marketsimcode. Run marketsim manually.")
    except Exception as e:
         print(f"\nAn error occurred during marketsim evaluation: {e}")


# Note: The check if __name__ == 'main': was incorrect in the original file.
# It should be if __name__ == "__main__":
# Corrected above in the example run section.