'''
This will serve as a testing shell for the market analyzer and forecaster that I am planning to use for CL trading

There will be a breakdown of the following

-Data builder - Takes the raw csv file for the CL data, and produces the post-processed datasets that will be used for analysis

    -Window analysis - Breaks the raw data into windows which provide summaries of metrics
    -Trade opportunities - Highlights conditions where a buy or a sell would have resulted in significant profit and provides metrics
        for these moments
    -Reversal/Trend finder
'''

import os
import numpy as np
import pandas as pd
import src.util as util
import src.indicatorBuilder as ind
from src.data_processor import DataProcessor

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

def get_cl_df(cl_test_data="data/raw/test100k.csv"):
    #Get the data
    cl_test_data = util.get_cl_data(cl_test_data)
    features = ind.generate_features(cl_test_data)
    #print(features.head())
    return features


def get_processed_cl_df(input_path="data/raw/test100k.csv", 
                        output_path=None,
                        dataset_version="set_01",
                        threshold=0.08, 
                        horizon=576,
                        force_reprocess=False):
    """
    Get processed CL data using DataProcessor.
    
    This function checks if processed data already exists. If it does and 
    force_reprocess is False, it loads the existing file. Otherwise, it 
    runs the full processing pipeline.
    
    Args:
        input_path: Path to raw CSV file (default: data/raw/test100k.csv)
        output_path: Path for processed output (auto-generated if None)
        dataset_version: Which dataset configuration to use (default: 'set_01')
        threshold: Target threshold for significant price move (default 0.08 = 8%)
        horizon: Forward-looking window for target in bars (default 576 = 48 hours)
        force_reprocess: If True, reprocess even if output file exists
        
    Returns:
        pd.DataFrame: Processed DataFrame with ML-ready features
    """
    # Create processor instance with dataset version
    processor = DataProcessor(
        input_path=input_path, 
        output_path=output_path,
        dataset_version=dataset_version
    )
    
    # Check if processed file already exists
    if os.path.exists(processor.output_path) and not force_reprocess:
        print(f"Loading existing processed data from {processor.output_path}")
        try:
            if processor.output_path.endswith('.parquet'):
                return pd.read_parquet(processor.output_path)
            else:
                return pd.read_csv(processor.output_path, index_col=0, parse_dates=True)
        except Exception as e:
            print(f"Error loading file: {e}. Reprocessing...")
    
    # Run the processing pipeline
    return processor.process(threshold=threshold, horizon=horizon)


if __name__ == '__main__':
    
    # Old CL df raw data (using indicatorBuilder)
    print("=" * 60)
    print("OLD METHOD: Using indicatorBuilder")
    print("=" * 60)
    features = get_cl_df()
    print("Total records:", features.shape)
    print("Columns:", list(features.columns))
    print(features.head())
    
    print("\n")
    
    # New CL df processed data (using DataProcessor) - SET_01
    print("=" * 60)
    print("NEW METHOD: Using DataProcessor (set_01)")
    print("=" * 60)
    processed_features = get_processed_cl_df(dataset_version="set_01")
    print("Total records:", processed_features.shape)
    print("Columns:", list(processed_features.columns))
    print(processed_features.head())
