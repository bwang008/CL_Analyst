 # Data Bootstrap (seed + macro)
 
 This repository expects a shared data root (see `.env.example`) with the
 following structure:
 
 ```
 CL_DATA_ROOT/
   data/
     raw/
       cl-5m_bk.csv
       macro/
         fred_macro_data.csv
         cftc_cot_crude_oil.csv
     processed/
   models/
 ```
 
 ## Seed CSV (`cl-5m_bk.csv`)
 
 The seed CSV is not in git. You must provide it before running
 `src.live_execution.live_trader`.
 
 **Options** (pick one and document your preferred source):
 
 - Copy from a trusted backup or internal share.
 - Download from your storage bucket (if applicable).
 - Rebuild from your raw data pipeline.
 
 Place the file at:
 
 ```
 ${CL_DATA_ROOT}/data/raw/cl-5m_bk.csv
 ```
 
 ## Macro CSVs (required for HourSet/set_07 models)
 
 Hourly and set_07-style models expect macro features. You can generate
 the macro CSVs using:
 
 ```bash
 python scripts/download_macro_data.py
 ```
 
 This writes:
 
 - `${CL_DATA_ROOT}/data/raw/macro/fred_macro_data.csv`
 - `${CL_DATA_ROOT}/data/raw/macro/cftc_cot_crude_oil.csv`
 
 FRED downloads require `FRED_API_KEY` in `.env` or the environment.
