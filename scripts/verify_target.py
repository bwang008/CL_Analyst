import argparse
from typing import Optional

import pandas as pd


def load_raw_ohlcv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, sep=None, engine="python", header=None)
    df.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]
    df["DateTime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"], format="%d/%m/%Y %H:%M"
    )
    df = df.drop(columns=["Date", "Time"]).set_index("DateTime")
    return df.sort_index()


def load_processed(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.set_index("DateTime")
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.sort_index()


def get_target_column(df: pd.DataFrame) -> Optional[str]:
    if "TARGET_Direction" in df.columns:
        return "TARGET_Direction"
    if "Target" in df.columns:
        return "Target"
    return None


def compute_future_moves(
    raw_df: pd.DataFrame, timestamp: pd.Timestamp, horizon: int
) -> dict:
    if timestamp not in raw_df.index:
        return {}
    row = raw_df.loc[timestamp]
    future = raw_df.loc[timestamp:].iloc[1 : horizon + 1]
    if future.empty:
        return {
            "close": row["Close"],
            "future_high": None,
            "future_low": None,
            "up_move": None,
            "down_move": None,
            "min_low_time": None,
        }
    future_high = future["High"].max()
    future_low = future["Low"].min()
    min_low_time = future["Low"].idxmin()
    close = row["Close"]
    up_move = (future_high - close) / close
    down_move = (close - future_low) / close
    return {
        "close": close,
        "future_high": future_high,
        "future_low": future_low,
        "up_move": up_move,
        "down_move": down_move,
        "min_low_time": min_low_time,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify target label vs raw data over a forward horizon."
    )
    parser.add_argument(
        "--raw",
        default="data/raw/test100k.csv",
        help="Path to raw CSV (default: data/raw/test100k.csv)",
    )
    parser.add_argument(
        "--processed",
        default="data/processed/test100k_set_01.csv",
        help="Path to processed CSV (default: data/processed/test100k_set_01.csv)",
    )
    parser.add_argument(
        "--timestamp",
        required=True,
        help="Timestamp to inspect (e.g., '2009-01-07 03:00:00')",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=576,
        help="Forward window in bars (default: 576 for 48h)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.08,
        help="Move threshold for Buy/Sell (default: 0.08 = 8%%)",
    )
    args = parser.parse_args()

    raw_df = load_raw_ohlcv(args.raw)
    proc_df = load_processed(args.processed)
    target_col = get_target_column(proc_df)

    ts = pd.to_datetime(args.timestamp)

    print(f"Raw file: {args.raw}")
    print(f"Processed file: {args.processed}")
    print(f"Timestamp: {ts}")
    print(f"Horizon: {args.horizon} bars, Threshold: {args.threshold:.2%}")

    if ts in raw_df.index:
        raw_row = raw_df.loc[ts]
        print(
            "Raw OHLCV:",
            f"Open={raw_row['Open']}",
            f"High={raw_row['High']}",
            f"Low={raw_row['Low']}",
            f"Close={raw_row['Close']}",
            f"Volume={raw_row['Volume']}",
        )
    else:
        print("Raw row: NOT FOUND for this timestamp.")

    if ts in proc_df.index and target_col:
        print(f"Processed target ({target_col}): {proc_df.loc[ts, target_col]}")
    elif ts in proc_df.index:
        print("Processed target: column not found (TARGET_Direction or Target).")
    else:
        print("Processed row: NOT FOUND for this timestamp.")

    moves = compute_future_moves(raw_df, ts, args.horizon)
    if not moves:
        print("No forward window available to compute future moves.")
        return

    print(f"Future high: {moves['future_high']}")
    print(f"Future low: {moves['future_low']} at {moves['min_low_time']}")
    print(f"Up move: {moves['up_move']:.4%}" if moves["up_move"] is not None else "Up move: n/a")
    print(
        f"Down move: {moves['down_move']:.4%}"
        if moves["down_move"] is not None
        else "Down move: n/a"
    )

    if moves["down_move"] is not None and moves["up_move"] is not None:
        if moves["up_move"] > args.threshold and moves["down_move"] > args.threshold:
            label = 1 if moves["up_move"] >= moves["down_move"] else 2
        elif moves["up_move"] > args.threshold:
            label = 1
        elif moves["down_move"] > args.threshold:
            label = 2
        else:
            label = 0
        print(f"Computed target: {label} (0=Hold,1=Buy,2=Sell)")


if __name__ == "__main__":
    main()
