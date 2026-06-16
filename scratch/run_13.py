import sys
sys.path.append('.')
from src.data_processor import DataProcessor
import os

os.environ["CL_DATA_ROOT"] = "C:\\CL_Analyst_Data"

print("Running HourSet_13A...")
processor_a = DataProcessor(input_path="C:\\CL_Analyst_Data\\data\\raw\\CL.csv", dataset_version="HourSet_13A")
processor_a.process()

print("\n\nRunning HourSet_13B...")
processor_b = DataProcessor(input_path="C:\\CL_Analyst_Data\\data\\raw\\CL.csv", dataset_version="HourSet_13B")
processor_b.process()
