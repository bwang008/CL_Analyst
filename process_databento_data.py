import pandas as pd
from databento_request import back_adjust_continuous_data

def process_downloaded_file():
    # The path to the raw CSV you downloaded and extracted
    raw_csv_path = r"C:\CL_Analyst_Data\data\raw\DataBentoSample\glbx-mdp3-20100606-20260613.ohlcv-1h.csv"
    
    # The path where you want to save the final cleaned and adjusted data
    # (Feel free to change this output path to wherever you prefer)
    output_csv_path = r"C:\CL_Analyst_Data\data\raw\DataBentoSample\adjusted_CL_history.csv"
    
    print(f"Loading raw data from:\n{raw_csv_path}")
    
    try:
        # Load the raw CSV into a Pandas DataFrame
        df = pd.read_csv(raw_csv_path)
        
        print("Applying datetime conversion, decimal division, and ratio back-adjustments...")
        # Pass the raw dataframe through the function we built earlier
        df_adjusted = back_adjust_continuous_data(df)
        
        # Save the finalized dataframe back to a new CSV file
        df_adjusted.to_csv(output_csv_path, index=False)
        print(f"Success! Clean adjusted data saved to:\n{output_csv_path}")
        
    except FileNotFoundError:
        print(f"Error: Could not find the file at {raw_csv_path}. Please make sure the path is correct.")

if __name__ == "__main__":
    process_downloaded_file()
