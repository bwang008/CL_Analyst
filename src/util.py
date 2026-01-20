"""MLT: Utility code.  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
Copyright 2017, Georgia Tech Research Corporation  		  	   		 	 	 			  		 			     			  	 
Atlanta, Georgia 30332-0415  		  	   		 	 	 			  		 			     			  	 
All Rights Reserved  		  	   		 	 	 			  		 			     			  	 
"""  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
import os  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
import pandas as pd  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def symbol_to_path(symbol, base_dir=None):  		  	   		 	 	 			  		 			     			  	 
    """Return CSV file path given ticker symbol."""  		  	   		 	 	 			  		 			     			  	 
    if base_dir is None:  		  	   		 	 	 			  		 			     			  	 
        base_dir = os.environ.get("MARKET_DATA_DIR", "data/")  		  	   		 	 	 			  		 			     			  	 
    return os.path.join(base_dir, "{}.csv".format(str(symbol)))  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def get_data(symbols, dates, addSPY=True, colname="Adj Close"):  		  	   		 	 	 			  		 			     			  	 
    """Read stock data (adjusted close) for given symbols from CSV files."""  		  	   		 	 	 			  		 			     			  	 
    df = pd.DataFrame(index=dates)  		  	   		 	 	 			  		 			     			  	 
    if addSPY and "SPY" not in symbols:  # add SPY for reference, if absent  		  	   		 	 	 			  		 			     			  	 
        symbols = ["SPY"] + list(  		  	   		 	 	 			  		 			     			  	 
            symbols  		  	   		 	 	 			  		 			     			  	 
        )  # handles the case where symbols is np array of 'object'  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
    for symbol in symbols:
        df_temp = pd.read_csv(
            symbol_to_path(symbol),
            skiprows=[1, 2],  # Skip the ticker row and the "Date" row
            index_col=0,  # Use first column as index
            parse_dates=True,
            na_values=["nan"],
        )
        
        # After skipping rows, the column names are the actual data values from the first row
        # We need to map the colname to the correct column index
        # The CSV structure is: Price,Close,High,Low,Open,Volume
        # After skipping rows, the columns become: Close,High,Low,Open,Volume (indices 0-4)
        column_mapping = {
            'Close': 0,  # First column (index 0)
            'High': 1,   # Second column (index 1)
            'Low': 2,    # Third column (index 2)
            'Open': 3,   # Fourth column (index 3)
            'Volume': 4, # Fifth column (index 4)
            'Adj Close': 0,  # Map Adj Close to Close column
        }
        
        if colname in column_mapping:
            col_index = column_mapping[colname]
            print(f"DEBUG: Looking for column '{colname}' at index {col_index}")
            print(f"DEBUG: df_temp has {len(df_temp.columns)} columns")
            print(f"DEBUG: df_temp columns: {list(df_temp.columns)}")
            if col_index < len(df_temp.columns):
                col_found = col_index
                print(f"DEBUG: Found column '{colname}' at index {col_index}")
            else:
                print(f"Warning: Column index {col_index} for '{colname}' not found in {symbol}.csv. Available columns: {list(df_temp.columns)}")
                continue
        else:
            print(f"Warning: Column '{colname}' not supported. Supported columns: {list(column_mapping.keys())}")
            continue
            
        df_temp = df_temp.iloc[:, [col_found]]  # Keep only the needed column by index
        df_temp = df_temp.rename(columns={df_temp.columns[0]: symbol})
        df = df.join(df_temp)
        if symbol == "SPY":  # drop dates SPY did not trade
            df = df.dropna(subset=["SPY"])

    return df  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def plot_data(df, title="Stock prices", xlabel="Date", ylabel="Price"):  		  	   		 	 	 			  		 			     			  	 
    import matplotlib.pyplot as plt  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
    """Plot stock prices with a custom title and meaningful axis labels."""  		  	   		 	 	 			  		 			     			  	 
    ax = df.plot(title=title, fontsize=12)  		  	   		 	 	 			  		 			     			  	 
    ax.set_xlabel(xlabel)  		  	   		 	 	 			  		 			     			  	 
    ax.set_ylabel(ylabel)  		  	   		 	 	 			  		 			     			  	 
    plt.show()  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def get_orders_data_file(basefilename):  		  	   		 	 	 			  		 			     			  	 
    return open(  		  	   		 	 	 			  		 			     			  	 
        os.path.join(  		  	   		 	 	 			  		 			     			  	 
            os.environ.get("ORDERS_DATA_DIR", "orders/"), basefilename  		  	   		 	 	 			  		 			     			  	 
        )  		  	   		 	 	 			  		 			     			  	 
    )  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def get_learner_data_file(basefilename):  		  	   		 	 	 			  		 			     			  	 
    return open(  		  	   		 	 	 			  		 			     			  	 
        os.path.join(  		  	   		 	 	 			  		 			     			  	 
            os.environ.get("LEARNER_DATA_DIR", "Data/"), basefilename  		  	   		 	 	 			  		 			     			  	 
        ),  		  	   		 	 	 			  		 			     			  	 
        "r",  		  	   		 	 	 			  		 			     			  	 
    )  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
def get_robot_world_file(basefilename):  		  	   		 	 	 			  		 			     			  	 
    return open(  		  	   		 	 	 			  		 			     			  	 
        os.path.join(  		  	   		 	 	 			  		 			     			  	 
            os.environ.get("ROBOT_WORLDS_DIR", "testworlds/"), basefilename  		  	   		 	 	 			  		 			     			  	 
        )  		  	   		 	 	 			  		 			     			  	 
    )  		  	   		 	 	 			  		 			     			  	 


def get_cl_data(data_file='data/raw/test10k.csv'):
    """
    Read OHLCV intraday data from CSV file which has no header row.
    Automatically detects separator (semicolon, comma, or tab) and applies appropriate formatting.
    Applies the same formatting shown in main.py for similar files:
      - header=None
      - assign columns: ['Date','Time','Open','High','Low','Close','Volume']
    - Creates proper datetime index for time-based operations

    Returns a pandas DataFrame containing the CSV contents with datetime index.
    """
    # Try different separators in order of likelihood
    separators = [';', ',', '\t']
    
    for sep in separators:
        try:
            # Read a small sample to test the separator
            sample_df = pd.read_csv(data_file, sep=sep, header=None, nrows=5)
            
            # Check if we got the expected 7 columns
            if sample_df.shape[1] == 7:
                # This separator works, read the full file
                df = pd.read_csv(data_file, sep=sep, header=None, index_col=None)
                df.columns = ['Date', 'Time', 'Open', 'High', 'Low', 'Close', 'Volume']
                
                # Combine Date and Time columns and parse as datetime
                df['DateTime'] = pd.to_datetime(df['Date'] + ' ' + df['Time'], format='%d/%m/%Y %H:%M')
                
                # Set as index and drop the separate Date and Time columns
                df.set_index('DateTime', inplace=True)
                df.drop(['Date', 'Time'], axis=1, inplace=True)
                
                print(f"Successfully read {data_file} using separator: '{sep}'")
                return df
                
        except Exception as e:
            continue  # Try next separator
    
    # If we get here, none of the separators worked
    raise ValueError(f"Could not read {data_file} with any of the separators: {separators}. "
                    f"Please check the file format.")
