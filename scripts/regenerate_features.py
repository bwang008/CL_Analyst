import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data_processor import DataProcessor

input_csv = r"C:\CL_Analyst_Data\data\raw\CL.csv"

def generate_set(dataset_version):
    print(f"Generating features for {dataset_version}...")
    processor = DataProcessor(
        input_path=input_csv,
        dataset_version=dataset_version,
        keep_ohlcv=True,
    )
    # the process method expects threshold and horizon, but for HourSets they might be hardcoded inside
    # let's just call process()
    processor.process()
    print(f"Finished generating features for {dataset_version}")

if __name__ == "__main__":
    generate_set("HourSet_09")
    generate_set("HourSet_10")
