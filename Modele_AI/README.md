# H2S Modele_AI

This directory contains the machine learning pipeline for predicting future energy consumption, renewable energy production, and battery state-of-charge. 

## Directory Structure

```text
Modele_AI/
├── energy_minute1.csv         # Raw energy consumption data (part 1)
├── energy_minute2.csv         # Raw energy consumption data (part 2)
├── energy_minute3.csv         # Raw energy consumption data (part 3)
├── energy_minute4.csv         # Raw energy consumption data (part 4)
├── open-meteo.csv             # Raw meteorological and weather data
├── df_final.csv               # Processed and aggregated hourly data
├── model.py                   # Production-ready Python pipeline for training & visualization
├── model.ipynb                # Legacy interactive Jupyter Notebook
└── trained_models/            # Output directory for models and visualizations
    ├── lgbm_model_1h.joblib   # Trained LightGBM model for 1-hour ahead predictions
    ├── lgbm_model_24h.joblib  # Trained LightGBM model for 24-hours ahead predictions
    ├── predictions_plot_1H.png  # Visualization of 1h prediction results
    └── predictions_plot_24H.png # Visualization of 24h prediction results
```

## Model Accuracies

The models use a `TimeSeriesSplit` cross-validation strategy, avoiding random shuffling to prevent data leakage from the future into the past. Here are the baseline accuracies (R² scores) evaluated on the hold-out test set using default LightGBM hyperparameters:

### 1-Hour Ahead Predictions (1H)
- **energy_total_wh**: R² = 0.139
- **production_wh**: R² = 0.739
- **battery_soc**: R² = 0.799

### 24-Hour Ahead Predictions (24H)
- **energy_total_wh**: R² = 0.258
- **production_wh**: R² = 0.645
- **battery_soc**: R² = 0.133

## Action Items to Improve Accuracy

The models currently do well with predicting production and battery states but struggle with `energy_total_wh` (total energy consumption) due to its high variance and reliance on human behavior. 

Here are the planned action items to significantly improve the accuracy:

1. **Hyperparameter Optimization**: The current results use sensible defaults. Re-enable the `optuna` block (by setting `use_optuna=True` in `model.py`) and increase `N_TRIALS` to 100+ to systematically hunt for the best tree depths, learning rates, and regularization penalties.
2. **Incorporate Temperature/Weather Data**: Energy consumption is highly correlated with external temperature (heating in winter, AC in summer). We need to fetch and merge temperature, humidity, and cloud cover metrics from the Open-Meteo API.
3. **Advanced Feature Engineering**:
    - **Cyclical Encoding**: We have sine/cosine embeddings for time, but we should add interaction features (e.g., `hour_sin * is_weekend`) to distinguish weekend routines.
    - **Exponential Moving Averages (EMA)**: Supplement the simple moving averages with EMA features, which give more weight to recent data points.
4. **Target Transformations**: Total energy consumption data is often highly skewed. Wrapping the regressor in a `TransformedTargetRegressor` (e.g., applying `np.log1p()` to the target before training and reversing it after) can help stabilize variance and handle spikes better.
5. **Feature Importance Analysis**: Generate SHAP plots or use LightGBM's native `plot_importance` to identify which lags and features are contributing to noise, and prune the feature space accordingly.
6. **Time-Series Differencing**: Instead of predicting the absolute future value, train the model to predict the *difference* or *change* (ΔT). Predicting `energy_T+1 - energy_T` can often yield better stationary results in heavily trending data.
