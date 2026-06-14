import os
import logging
from datetime import datetime
import pandas as pd
import databento as db

log = logging.getLogger(__name__)

class DatabentoDataBuilder:
    """Fetches historical continuous data from Databento and applies ratio back-adjustment."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("DATABENTO_API_KEY")
        if not self.api_key:
            raise ValueError("DATABENTO_API_KEY not found in environment.")
        self.client = db.Historical(self.api_key)

    def submit_historical_batch(
        self,
        dataset: str = "GLB.CME",
        symbols: str | list[str] = "CL.v.0",
        start: str | pd.Timestamp = "2011-01-01",
        end: str | pd.Timestamp = "2026-06-01",
        schema: str = "ohlcv-1h",
    ):
        """Submit a batch job to Databento for historical data."""
        log.info(f"Submitting batch job for {symbols} from {start} to {end}...")
        job = self.client.timeseries.submit_job(
            dataset=dataset,
            symbols=symbols,
            schema=schema,
            start=start,
            end=end,
            encoding="csv",
            delivery="download",
            stype_in="continuous",
        )
        log.info(f"Job submitted successfully. Job ID: {job['id']}")
        return job

    def process_and_adjust_data(self, file_path: str) -> pd.DataFrame:
        """
        Load unadjusted continuous data and apply Ratio (Proportional) Back-Adjustment.
        Anchors the multiplier to the most recent front-month contract.
        """
        log.info(f"Loading unadjusted data from {file_path}")
        # Databento CSVs typically have 'ts_event', 'symbol', 'open', 'high', 'low', 'close', 'volume'
        df = pd.read_csv(file_path, parse_dates=["ts_event"])
        df = df.sort_values("ts_event").reset_index(drop=True)
        
        # Identify roll dates by checking when 'symbol' changes
        if "symbol" not in df.columns:
            log.warning("No 'symbol' column found. Returning raw dataframe.")
            return df

        df["roll_flag"] = df["symbol"] != df["symbol"].shift(1)
        # The first row is always a "change" technically, so ignore it
        df.loc[0, "roll_flag"] = False

        # Calculate ratio multipliers going backward from the most recent data
        # We need the close price of the old contract and the new contract at the roll boundary.
        # But wait, Databento continuous series provides a single series. 
        # A roll happens between bar t-1 (old symbol) and bar t (new symbol).
        # We need to compute the price gap. Since we only have the continuous stitched data,
        # we can't easily see both prices at the exact same timestamp unless we query the raw expirations.
        # However, for a simple approximation over continuous data:
        # If the gap is purely due to roll (which usually happens over a weekend or overnight), 
        # ratio = Close_t / Close_t-1 (Wait, normally it's new / old or old / new).
        # Because we only have the single continuous stream, true ratio adjustment requires 
        # querying the overlapping day for both contracts. 
        # Assuming Databento continuous series provides just the spliced bars, 
        # we'll implement the structure for ratio adjustment. True adjustment requires
        # standardizing the roll gap vs market gap, but for now we provide the ratio adjustment skeleton
        # as requested for the data pipeline.
        
        df["multiplier"] = 1.0
        
        # Get indices where rolls occurred
        roll_indices = df[df["roll_flag"]].index.tolist()
        
        # Iterate backwards
        current_multiplier = 1.0
        for i in reversed(roll_indices):
            # i is the first index of the new contract
            # i-1 is the last index of the old contract
            # To anchor to the most recent, we adjust older contracts.
            # Ratio = Close(new, t) / Close(old, t). Since we don't have overlapping t,
            # we use Close(new, i) and Close(old, i-1).
            # Note: This includes the overnight market return which is not ideal,
            # but is the standard fallback when overlap data isn't provided.
            new_close = df.loc[i, "close"]
            old_close = df.loc[i-1, "close"]
            
            if old_close != 0:
                ratio = new_close / old_close
                current_multiplier *= ratio
            
            # Apply to all rows before the roll
            df.loc[:i-1, "multiplier"] = current_multiplier
            
        log.info("Applying ratio adjustment to OHLC...")
        df["open"] = df["open"] * df["multiplier"]
        df["high"] = df["high"] * df["multiplier"]
        df["low"] = df["low"] * df["multiplier"]
        df["close"] = df["close"] * df["multiplier"]
        
        # Volume is typically NOT adjusted in ratio-adjustment, but sometimes it's inversely adjusted.
        # Standard practice leaves Volume unadjusted.
        
        # Drop temporary columns
        df = df.drop(columns=["roll_flag", "multiplier"])
        
        return df

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # builder = DatabentoDataBuilder()
    # builder.submit_historical_batch()
