from src.data_processor import DataProcessor
import sys

if __name__ == "__main__":
    print("Starting data generation...", flush=True)
    dp = DataProcessor(
        input_path=r'C:\CL_Analyst_Data\data\raw\cl-5m_bk.csv',
        dataset_version='set_12'
    )
    df = dp.process()
    print("Finished data generation.", flush=True)
