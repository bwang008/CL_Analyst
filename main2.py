# main_script.py
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, accuracy_score, roc_auc_score

# --- Import your custom modules ---
# The script now assumes you have these .py files in your project directory.
import src.util as util
import src.indicatorBuilder as ind

if __name__ == '__main__':
    
    # --- 1. Get and Prepare Data ---
    # This now uses the structure you provided, calling your actual files.
    cl_data = util.get_cl_data('data/test100k.csv')
    print("--- Original Data Head ---")
    print(cl_data.head())
    
    # --- 2. Generate the Indicator Features ---
    features_with_indicators = ind.generate_features(cl_data)
    print("\n--- Data with Features Head ---")
    # Increase the number of rows printed to ensure you see non-NaN indicator values
    
    print(features_with_indicators.head(30)) 
    
    # --- 3. Define Target Variable (y) and Features (X) ---
    
    # --- OPTION 1: REGRESSION (Predict next closing price) ---
    # To use this, uncomment the two lines below and comment out the Classification block.
    # TARGET_MODE = 'regression'
    # features_with_indicators['target'] = features_with_indicators['Close'].shift(-1)
    
    # --- OPTION 2: CLASSIFICATION (Predict >5% price change in next 3 days) ---
    # This is the currently active mode.
    TARGET_MODE = 'classification'
    # Calculate the number of 5-minute periods in 3 days
    periods_in_3_days = 12 * 24 * 3  # (12 5-min periods per hour) * (24 hours) * (3 days)
    # Look ahead to get the price 3 days from now
    future_price = features_with_indicators['Close'].shift(-periods_in_3_days)
    # Calculate the percentage change
    price_change_pct = (future_price - features_with_indicators['Close']) / features_with_indicators['Close']
    # Set the target: 1 if the absolute change is > 5%, otherwise 0
    features_with_indicators['target'] = (np.abs(price_change_pct) > 0.05).astype(int)

    # --- Data Cleaning and Finalizing X and y ---
    # Drop rows with any NaN values. This is crucial as NaNs are created by:
    # 1. Initial indicator calculations (e.g., a 20-period SMA is NaN for the first 19 rows).
    # 2. The target variable lookahead (the last 'periods_in_3_days' rows will be NaN).
    final_df = features_with_indicators.dropna()
    
    # Define features (X) and target (y)
    X = final_df.drop(columns=['Open', 'High', 'Low', 'Close', 'Volume', 'target'])
    y = final_df['target']

    print("\n--- Final Features (X) Head ---")
    print(X.head())
    print("\n--- Final Target (y) Head ---")
    print(y.head())
    print(f"\nTarget Mode: {TARGET_MODE}")

    if TARGET_MODE == 'classification':
        print("Target Distribution (0s and 1s):")
        # Check for class imbalance. If one class is rare, you may need techniques like SMOTE.
        print(y.value_counts(normalize=True))


    # --- 4. Split Data into Training and Testing Sets ---
    # For time series, we perform a chronological split to avoid data leakage.
    test_size = 0.2
    split_index = int(len(X) * (1 - test_size))
    X_train, X_test = X[:split_index], X[split_index:]
    y_train, y_test = y[:split_index], y[split_index:]
    
    print(f"\nTraining set size: {len(X_train)} samples")
    print(f"Testing set size: {len(X_test)} samples")

    # --- 5. Create and Train the LightGBM Model ---
    if TARGET_MODE == 'regression':
        print("\n--- Training LightGBM REGRESSOR ---")
        model = lgb.LGBMRegressor(objective='regression', metric='rmse', n_estimators=1000, seed=42)
        model.fit(X_train, y_train,
                  eval_set=[(X_test, y_test)],
                  eval_metric='rmse',
                  callbacks=[lgb.early_stopping(100, verbose=True)])
        
        # --- 6. Make Predictions and Evaluate ---
        print("\n--- Making Predictions on Test Data ---")
        predictions = model.predict(X_test)
        rmse = np.sqrt(mean_squared_error(y_test, predictions))
        print(f"\nModel Evaluation (RMSE): {rmse:.4f}")
        
        results = pd.DataFrame({'Actual': y_test, 'Predicted': predictions})
        print("\n--- Sample of Predictions vs Actual Values ---")
        print(results.head(10))

    elif TARGET_MODE == 'classification':
        print("\n--- Training LightGBM CLASSIFIER ---")
        # is_unbalance=True can be helpful if the target classes are imbalanced.
        model = lgb.LGBMClassifier(objective='binary', metric='logloss', n_estimators=1000, seed=42, is_unbalance=True)
        model.fit(X_train, y_train,
                  eval_set=[(X_test, y_test)],
                  eval_metric='logloss',
                  callbacks=[lgb.early_stopping(100, verbose=True)])

        # --- 6. Make Predictions and Evaluate ---
        print("\n--- Making Predictions on Test Data ---")
        predictions = model.predict(X_test)
        pred_probs = model.predict_proba(X_test)[:, 1]  # type: ignore # Probabilities for the '1' class
        
        accuracy = accuracy_score(y_test, predictions)
        auc = roc_auc_score(y_test, pred_probs)
        
        print(f"\nModel Evaluation:")
        print(f"Accuracy: {accuracy:.4f}")
        print(f"AUC (Area Under Curve): {auc:.4f}")
        
        results = pd.DataFrame({'Actual': y_test, 'Predicted_Class': predictions, 'Predicted_Prob_of_1': pred_probs})
        print("\n--- Sample of Predictions vs Actual Values ---")
        print(results.head(10))
