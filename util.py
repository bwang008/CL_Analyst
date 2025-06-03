#My own version of util.py
import pandas as pd
import numpy as np
import os
import yfinance as yf

def get_data(symbols, dates, addSPY=True, colname='Close'):
    """
    Read stock data (adjusted close) for given symbols from CSV files.
    If addSPY is True, add SPY data to the DataFrame.
    If the data file for a symbol does not exist, download it using yfinance.
    """
    print(f"Getting data for symbols: {symbols}")
    print(f"Date range: {dates[0]} to {dates[-1]}")
    
    # Create an empty DataFrame to hold the data
    df = pd.DataFrame(index=dates)
    print(f"Initial empty DataFrame shape: {df.shape}")

    # Read data for each symbol and join it to the DataFrame
    for symbol in symbols:
        try:
            # Check if historical data exists for the symbol
            data_file = f'data/{symbol}.csv'
            if not os.path.exists(data_file):
                print(f"Historical data for {symbol} not found. Downloading using yfinance...")
                # Download historical data using yfinance
                stock_data = yf.download(symbol, start="2000-01-01", end="2023-12-31")
                # Save the data to a CSV file
                stock_data.to_csv(data_file)
                print(f"Data for {symbol} saved to {data_file}.")
            else:
                print(f"Historical data for {symbol} already exists at {data_file}.")

            # Load the data
            print(f"Reading {data_file}")
            df_temp = pd.read_csv(data_file, index_col="Date", parse_dates=True, skiprows=[1], na_values=["nan"])
            print(f"Raw data shape: {df_temp.shape}")
            print(f"Raw data columns: {df_temp.columns}")
            
            # Use the specified column as the price
            df_temp = df_temp[[colname]]
            df_temp.rename(columns={colname: symbol}, inplace=True)
            print(f"Processed data shape: {df_temp.shape}")
            print(f"Processed data columns: {df_temp.columns}")
            
            df = df.join(df_temp, how='inner')
            print(f"After join shape: {df.shape}")
            print(f"After join columns: {df.columns}")
            
        except FileNotFoundError:
            print(f"File for {symbol} not found.")
        except Exception as e:
            print(f"An error occurred while processing {symbol}: {e}")
            print(f"Error type: {type(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")

    # Add SPY data if required
    if addSPY and 'SPY' not in df.columns:
        try:
            data_file = "data/SPY.csv"
            if not os.path.exists(data_file):
                print("Historical data for SPY not found. Downloading using yfinance...")
                stock_data = yf.download("SPY", start="2000-01-01", end="2023-12-31")
                stock_data.to_csv(data_file)
                print(f"Data for SPY saved to {data_file}.")
            else:
                print(f"Historical data for SPY already exists at {data_file}.")

            df_SPY = pd.read_csv(data_file, index_col='Date', parse_dates=True, skiprows=[1], na_values=['nan'])
            df_SPY = df_SPY[[colname]]
            df_SPY.rename(columns={colname: 'SPY'}, inplace=True)
            df = df.join(df_SPY, how='inner')
        except FileNotFoundError:
            print("File for SPY not found.")
        except Exception as e:
            print(f"An error occurred while processing SPY: {e}")

    # Fill any missing values with 0
    df.fillna(0, inplace=True)
    print(f"Final DataFrame shape: {df.shape}")
    print(f"Final DataFrame columns: {df.columns}")

    return df