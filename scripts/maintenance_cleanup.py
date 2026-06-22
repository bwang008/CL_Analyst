import os
import argparse
import time
import json
from pathlib import Path

def get_protected_sweeps(root_dir: Path) -> set:
    """Scan all batch_runs configs and extract the sweep folders they reference."""
    protected = set()
    batch_runs_dir = root_dir / 'reports' / 'batch_runs'
    
    if not batch_runs_dir.exists():
        return protected

    for config_file in batch_runs_dir.rglob('*.json'):
        try:
            with open(config_file, 'r') as f:
                data = json.load(f)
                
            # Naive recursive search for anything that looks like a sweep path
            def extract_sweeps(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        extract_sweeps(v)
                elif isinstance(obj, list):
                    for item in obj:
                        extract_sweeps(item)
                elif isinstance(obj, str):
                    if 'sweep_' in obj:
                        # Extract the sweep folder name (e.g. sweep_hs13a_4x1_36h_20260621_0114)
                        parts = Path(obj).parts
                        for part in parts:
                            if part.startswith('sweep_'):
                                protected.add(part)
                                break
            
            extract_sweeps(data)
        except Exception:
            pass
            
    return protected

def main():
    parser = argparse.ArgumentParser(description="Clean up unreferenced sweep artifacts.")
    parser.add_argument("--days", type=int, default=3, help="Delete files older than this many days.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be deleted without actually deleting.")
    args = parser.parse_args()

    now = time.time()
    cutoff_time = now - (args.days * 86400)
    total_freed = 0
    files_to_delete = []
    
    root_dir = Path(__file__).resolve().parent.parent
    reports_dir = root_dir / 'reports'
    predictions_dir = root_dir / 'data' / 'predictions'

    # 1. Identify protected sweeps
    protected_sweeps = get_protected_sweeps(root_dir)
    print(f"Protected sweeps found in batch_runs: {len(protected_sweeps)}")

    # 2. Find heavy artifacts in unprotected sweeps
    heavy_extensions = {'.zip', '.pkl', '.h5', '.pt', '.safetensors', '.onnx', '.model', '.journal'}
    if reports_dir.exists():
        for sweep_dir in reports_dir.glob('sweep_*'):
            if not sweep_dir.is_dir():
                continue
                
            # Check for manual keep file
            if (sweep_dir / '.keep').exists():
                print(f"Skipping {sweep_dir.name} (Protected by .keep file)")
                continue
                
            # Check if referenced in batch_runs
            if sweep_dir.name in protected_sweeps:
                print(f"Skipping {sweep_dir.name} (Protected by batch_runs)")
                continue

            # It's an unprotected "loser" sweep. Target heavy files for deletion.
            for p in sweep_dir.rglob('*'):
                if p.is_file() and p.suffix in heavy_extensions:
                    if p.stat().st_mtime < cutoff_time:
                        files_to_delete.append(p)

    # 3. Clean up unreferenced predictions (any CSV not matching a protected sweep)
    if predictions_dir.exists():
        for p in predictions_dir.rglob('*.csv'):
            if p.is_file() and p.stat().st_mtime < cutoff_time:
                # If a sweep is protected, keep its prediction CSVs too!
                is_protected = False
                for sweep_name in protected_sweeps:
                    if sweep_name in p.name:
                        is_protected = True
                        break
                if not is_protected:
                    files_to_delete.append(p)

    # Calculate sizes and delete
    for p in files_to_delete:
        size = p.stat().st_size
        total_freed += size
        if args.dry_run:
            pass # Keep output quiet for dry-run of thousands of files
        else:
            try:
                p.unlink()
            except Exception as e:
                print(f"Error deleting {p}: {e}")

    # 4. Truncate backtest log
    log_file = reports_dir / 'backtest_engine_log'
    if log_file.exists() and log_file.is_file():
        size = log_file.stat().st_size
        if size > 50 * 1024 * 1024:  # > 50 MB
            total_freed += (size - (5 * 1024 * 1024))
            if not args.dry_run:
                try:
                    with open(log_file, 'rb') as f:
                        f.seek(-5 * 1024 * 1024, os.SEEK_END)
                        tail = f.read()
                    with open(log_file, 'wb') as f:
                        f.write(tail)
                except Exception as e:
                    print(f"Error truncating log: {e}")

    print("\n" + "="*40)
    mode_str = "DRY-RUN SUMMARY" if args.dry_run else "CLEANUP SUMMARY"
    print(f"{mode_str} (Files older than {args.days} days)")
    print(f"Total files targeted: {len(files_to_delete)}")
    print(f"Total space saved: {total_freed / 1024 / 1024:.2f} MB")
    print("="*40)

if __name__ == "__main__":
    main()
