import sys
import os
import argparse
import json
from pathlib import Path
from pydantic import ValidationError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data_processor import DataProcessor
from src.config.schemas import MasterConfig

def main():
    parser = argparse.ArgumentParser(description="Config-driven Data Pipeline")
    parser.add_argument("--config", required=True, help="Path to Master Config JSON")
    parser.add_argument("--exec-data", default=None, help="Path to raw execution data")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        raw_json = json.load(f)

    try:
        master_config = MasterConfig(**raw_json)
    except ValidationError as e:
        print(f"Configuration Validation Error in {config_path.name}:\n{e}")
        sys.exit(1)

    if not master_config.data_workflow:
        print("Error: No data_workflow section found in Master Config.")
        sys.exit(1)

    print(f"Validated configuration for {master_config.symbol}")
    
    # Save a copy of the validated configuration for artifact lineage
    lineage_path = Path(PROJECT_ROOT) / "data" / "processed" / f"{master_config.symbol}_{master_config.data_workflow.dataset_version}_config.json"
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lineage_path, "w") as f:
        f.write(master_config.model_dump_json(indent=2))
        
    print(f"Saved artifact lineage to {lineage_path}")

    processor = DataProcessor(
        dataset_version=master_config.data_workflow.dataset_version,
        symbol=master_config.symbol,
        config=master_config.data_workflow,
        keep_ohlcv=True,
    )
    
    try:
        df = processor.process_from_config(exec_ohlcv_path=args.exec_data)
        
        print("\nFeature Summary:")
        print("-" * 40)
        print(df.describe().T.head(20)) # Print just the head so it doesn't flood the terminal
        
        out_dir = Path(PROJECT_ROOT) / master_config.data_workflow.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = master_config.data_workflow.output_filename or f"{master_config.symbol}_{master_config.data_workflow.dataset_version}.parquet"
        out_path = out_dir / filename
        df.to_parquet(out_path)
        print(f"\n[SUCCESS] Saved {len(df)} rows and {len(df.columns)} columns to {out_path}")
        
    except Exception as e:
        print(f"Error during processing: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
