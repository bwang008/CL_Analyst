import os
import sys
from pathlib import Path

# Resolve project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data_processor import DataProcessor
from src.data_paths import get_data_path

def main():
    raw_path = get_data_path("raw/cl-5m_bk.csv")
    print(f"Using raw data from: {raw_path}")
    
    # Initialize DataProcessor for Hour4Set_01
    processor = DataProcessor(
        input_path=str(raw_path),
        dataset_version="Hour4Set_01",
        keep_ohlcv=True
    )
    
    # Process
    print("Starting processing for Hour4Set_01...")
    df = processor.process()
    print("Processing complete!")
    print(f"Output saved to: {processor.output_path}")
    print(f"Data shape: {df.shape}")

if __name__ == "__main__":
    main()
