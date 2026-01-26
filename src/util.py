"""MLT: Utility code.  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
Copyright 2017, Georgia Tech Research Corporation  		  	   		 	 	 			  		 			     			  	 
Atlanta, Georgia 30332-0415  		  	   		 	 	 			  		 			     			  	 
All Rights Reserved  		  	   		 	 	 			  		 			     			  	 
"""  		  	   		 	 	 			  		 			     			  	 
  		  	   		 	 	 			  		 			     			  	 
import os  		 	 
import numpy as np

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


# =============================================================================
# ML Pipeline Utilities
# =============================================================================

# Column prefixes that should be excluded from ML features
EXCLUDED_PREFIXES = ('RAW_', 'TARGET_', 'META_')

# Explicit column names to exclude from ML features (legacy or non-feature cols)
EXCLUDED_COLUMNS = {'Target'}


def get_feature_columns(df: pd.DataFrame) -> list:
    """
    Returns column names that are valid ML features.
    
    Excludes columns starting with:
    - RAW_: Raw/diagnostic data for evaluation (e.g., RAW_Close, RAW_Future_High)
    - TARGET_: Target labels for training (e.g., TARGET_Direction)
    - META_: Metadata columns (e.g., META_Symbol)
    
    This is the SINGLE SOURCE OF TRUTH for what the model sees.
    All training/evaluation code should use this function to extract features.
    
    Args:
        df: DataFrame containing processed data with features, RAW_, and TARGET_ columns
        
    Returns:
        list: Column names that are valid ML features (no excluded prefixes)
        
    Example:
        >>> df.columns
        ['RSI', 'MACD', 'RAW_Close', 'RAW_Future_High', 'TARGET_Direction']
        >>> get_feature_columns(df)
        ['RSI', 'MACD']
    """
    return [
        col for col in df.columns
        if not col.startswith(EXCLUDED_PREFIXES) and col not in EXCLUDED_COLUMNS
    ]


def get_target_column(df: pd.DataFrame, target_name: str = 'TARGET_Direction') -> str:
    """
    Returns the target column name if it exists in the DataFrame.
    
    Args:
        df: DataFrame to check
        target_name: Expected target column name (default: 'TARGET_Direction')
        
    Returns:
        str: The target column name
        
    Raises:
        ValueError: If target column is not found in DataFrame
    """
    if target_name in df.columns:
        return target_name
    
    # Legacy fallback for older processed files
    if target_name == 'TARGET_Direction' and 'Target' in df.columns:
        return 'Target'
    
    target_cols = [c for c in df.columns if c.startswith('TARGET_')]
    raise ValueError(
        f"Target column '{target_name}' not found. "
        f"Available TARGET_ columns: {target_cols}"
    )


def get_X_y(df: pd.DataFrame, target_name: str = 'TARGET_Direction'):
    """
    Safely extract features (X) and target (y) from a DataFrame.
    
    This function ensures that only valid feature columns are used for X,
    preventing data leakage from RAW_ or other excluded columns.
    
    Args:
        df: DataFrame containing features and target
        target_name: Name of the target column (default: 'TARGET_Direction')
        
    Returns:
        tuple: (X, y) where X is a DataFrame of features and y is a Series of targets
        
    Example:
        >>> X, y = get_X_y(df)
        >>> X.columns  # Only feature columns, no RAW_ or TARGET_
        ['RSI', 'MACD', 'VOL_3D', ...]
    """
    feature_cols = get_feature_columns(df)
    target_col = get_target_column(df, target_name)
    
    X = df[feature_cols]
    y = df[target_col]
    
    return X, y


def downsample_majority(
    X: pd.DataFrame,
    y: pd.Series,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Downsample majority class for binary targets to achieve 50/50 balance.
    Keeps all minority samples and randomly samples the majority.
    """
    y_values = y.to_numpy()
    classes, counts = np.unique(y_values, return_counts=True)
    if len(classes) != 2:
        raise ValueError("downsample_majority only supports binary targets.")

    minority_class = classes[np.argmin(counts)]
    majority_class = classes[np.argmax(counts)]
    minority_idx = np.where(y_values == minority_class)[0]
    majority_idx = np.where(y_values == majority_class)[0]

    rng = np.random.default_rng(random_state)
    sampled_majority_idx = rng.choice(majority_idx, size=len(minority_idx), replace=False)
    keep_idx = np.concatenate([minority_idx, sampled_majority_idx])
    rng.shuffle(keep_idx)

    return X.iloc[keep_idx], y.iloc[keep_idx]
