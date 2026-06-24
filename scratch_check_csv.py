import pandas as pd

def check_csv(path):
    print(f"\nChecking {path}...")
    try:
        df = pd.read_csv(path, sep=';', header=None, nrows=5)
        print("First 5 rows:")
        print(df)
    except Exception as e:
        print(f"Error: {e}")

check_csv(r"C:\CL_Analyst_Data\data\raw\CL.csv")
check_csv(r"C:\CL_Analyst_Data\data\raw\cl-5m_bk.csv")
check_csv(r"C:\CL_Analyst_Data\data\raw\DataBentoSample\glbx-mdp3-20100606-20260613.ohlcv-1h.csv")
