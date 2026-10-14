import docx
from docx import Document
from docx.shared import Inches

def rewrite_docx(input_path, output_path):
    # Read original text
    doc_in = Document(input_path)
    old_text = [p.text for p in doc_in.paragraphs if p.text.strip()]
    
    doc = Document()
    doc.add_heading('MODELING REPORT — AI PREDICTION ENGINE (LightGBM & XGBoost)', 0)
    doc.add_paragraph('H2S Project — Energy Optimization through Artificial Intelligence — AI Prediction Engine')
    doc.add_heading('Executive Summary', 1)
    
    # 1. Introduction
    doc.add_heading('1. Introduction', 1)
    doc.add_heading('1.1. H2S Project context and objectives of Brick 4', 2)
    doc.add_paragraph(
        "The H2S project is part of the energy transition context, where the massive integration of intermittent "
        "renewable energies requires accurate forecasting systems to balance supply and demand. Unlike centralized "
        "approaches, H2S deploys a LightGBM model at the Edge (Raspberry Pi) for real-time local inference, while "
        "participating in federated learning to improve the global model without compromising client data confidentiality."
    )
    doc.add_heading('1.2. Challenges of short-term (1h) and mid-term (24h) forecasting', 2)
    doc.add_paragraph("Short-term forecasting (1h) is highly dependent on immediate sensor readings and rapid changes in human behavior. Mid-term forecasting (24h) requires robust meteorological predictions and reliable industrial scheduling, as local consumption patterns become harder to predict purely autoregressively.")
    
    doc.add_heading('1.3. Positioning of LightGBM within the hybrid predictive architecture', 2)
    doc.add_paragraph("LightGBM and XGBoost sit at the core of our Edge intelligence, processing real-time telemetry from Shelly plugs alongside weather APIs to deliver low-latency control signals.")
    
    doc.add_heading('1.4. Contributions and scope of the report', 2)
    doc.add_paragraph("This report details the data preprocessing pipelines, the feature engineering process, the model architectures, and an exhaustive evaluation of the forecasting performance for both renewable production and energy consumption.")
    
    # 2. State of the Art
    doc.add_heading('2. State of the Art and Rationale for LightGBM', 1)
    doc.add_heading('2.1. Overview of energy forecasting methods', 2)
    doc.add_paragraph("Statistical methods (ARIMA, exponential smoothing)")
    doc.add_paragraph("Machine Learning methods (RF, XGBoost, CatBoost)")
    doc.add_paragraph("Deep Learning methods (LSTM, GRU, Drop-GRU)")
    
    doc.add_heading('2.2. Comparison of reported performances in the literature', 2)
    doc.add_paragraph(
        "Recent studies have shown that tree ensemble models (XGBoost, LightGBM) outperform recurrent networks (LSTM, GRU) "
        "on tabular energy time-series data [1]. Mahjoub et al. (2022) compared LSTM, GRU, and Drop-GRU on French consumption "
        "data, with R² ranging from 0.70 to 0.97 depending on the horizon [2]. However, these models require significant "
        "computational resources, making them unsuitable for Edge deployment. LightGBM, with its GOSS (Gradient-based One-Side "
        "Sampling) technique, offers an optimal compromise between accuracy and lightness [3]."
    )
    
    doc.add_heading('2.3. Advantages of LightGBM: GOSS, EFB, speed, memory footprint', 2)
    doc.add_paragraph(
        "LightGBM was selected for its Gradient-based One-Side Sampling (GOSS) and Exclusive Feature Bundling (EFB), "
        "which drastically reduce training time and memory footprint compared to XGBoost, without sacrificing accuracy."
    )
    
    doc.add_heading('2.4. Relevance of LightGBM to H2S constraints (Edge + Cloud)', 2)
    doc.add_paragraph(
        "Its lightweight C++ inference engine allows scoring in < 1 ms on a Raspberry Pi, satisfying the real-time "
        "control requirements of the local rule engine."
    )
    
    # 3. Data and Preparation
    doc.add_heading('3. Data and Preparation', 1)
    doc.add_heading('3.1. Data sources', 2)
    doc.add_paragraph("The models ingest data from multiple streams:")
    doc.add_paragraph("1. Field sensors (Shelly plugs): 266768 raw records aggregated to a 1-minute grid.")
    doc.add_paragraph("2. Weather APIs: Open-Meteo for historical observations (ERA5) and archived NWP day-ahead forecasts (temperature, wind, radiation).")
    doc.add_paragraph("3. Electricity market: Energy-Charts API for day-ahead spot prices (FR bidding zone).")
    doc.add_paragraph("4. Industrial schedules (Roadmap): Planned 24h consumption profiles.")
    
    doc.add_heading('3.2. Exploratory Data Analysis (EDA)', 2)
    doc.add_paragraph("Analysis revealed short gaps in sensor telemetry.")
    doc.add_heading('3.3. Preprocessing', 2)
    doc.add_paragraph(
        "Short gaps (< 5 min) are linearly interpolated. Longer gaps are explicitly flagged as invalid to prevent training on "
        "artificial zeros. Missing target values are masked, while missing features during inference (e.g., NWP API outage) "
        "are handled natively by LightGBM or through EMA imputation."
    )
    
    doc.add_heading('3.4. Feature selection', 2)
    doc.add_paragraph(
        "Features include lagged variables, rolling statistics (EMA, standard deviations), cyclical calendar encodings "
        "(sine/cosine for hour, day, month), and derived physical priors (clear-sky radiation model, solar elevation). "
        "In Phase 3, we introduced explicit interaction terms (e.g., tgt_hour_sin_weekend) to help tree models cleanly divide human routines."
    )
    
    doc.add_heading('3.5. Train / validation / test split', 2)
    doc.add_paragraph("Chronological validation split to prevent future-data leakage.")
    
    # 4. Architecture and Training
    doc.add_heading('4. Architecture and Training of LightGBM Models', 1)
    doc.add_heading('4.1. Overview of produced models', 2)
    doc.add_paragraph(
        "Figure X presents the global architecture of the prediction pipeline, including the 8 LightGBM models, "
        "the data flow from Shelly sensors to Edge inference, and the integration with federated learning."
    )
    
    # Adding the table of 8 models
    table = doc.add_table(rows=1, cols=4)
    table.style = 'Table Grid'
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = 'Model'
    hdr_cells[1].text = 'What it predicts'
    hdr_cells[2].text = 'Horizon'
    hdr_cells[3].text = 'Main data source'
    
    models = [
        ('production_1h', 'Renewable production (solar + wind)', '1 hour', 'NWP weather + historical production'),
        ('production_24h', 'Renewable production', '24 hours', 'NWP weather + historical production'),
        ('consumption_1h', 'Total site consumption', '1 hour', 'Historical consumption + devices'),
        ('consumption_24h (device and fallback)', 'Total consumption (degraded mode)', '24 hours', 'Historical consumption + devices'),
        ('weather_1h', 'Weather (temperature, irradiance, wind)', '1 hour', 'NWP + historical weather'),
        ('weather_24h', 'Weather', '24 hours', 'NWP + historical weather'),
        ('price_1h', 'Electricity spot price', '1 hour', 'Historical price + calendar'),
        ('price_24h', 'Electricity spot price', '24 hours', 'Historical price + calendar')
    ]
    for m in models:
        row_cells = table.add_row().cells
        row_cells[0].text = m[0]
        row_cells[1].text = m[1]
        row_cells[2].text = m[2]
        row_cells[3].text = m[3]
    
    doc.add_heading('4.2. Parameters and hyperparameters', 2)
    doc.add_paragraph("Hyperparameters (learning_rate, max_depth, num_leaves, etc.) are tuned using Bayesian optimization (Optuna TPE).")
    
    doc.add_heading('4.3. Training strategy', 2)
    doc.add_paragraph("Models utilize early stopping (100 rounds) on the validation set. L1 and L2 regularization limit overfitting on consumption peaks. Expanding-window time-series cross-validation ensures structural stability.")
    
    doc.add_heading('4.4. Specificity of 24h consumption', 2)
    doc.add_paragraph("For industrial 24h prediction, the system relies primarily on declared operating schedules. The ML model acts as a degraded-mode backup if schedules are missing.")
    
    doc.add_heading('4.5. Hybrid approach: ML (production) + scheduling (consumption)', 2)
    
    prod_1h_r2, prod_1h_mae = 0.919, 2.70
    prod_24h_r2, prod_24h_mae = 0.812, 5.03
    cons_1h_r2, cons_1h_mae = 0.642, 13.94
    cons_1h_lgb, cons_1h_xgb = 0.653, 0.713
    prod_1h_lgb, prod_1h_xgb = 0.913, 0.903

    doc.add_heading('5. Evaluation and Results', 1)
    doc.add_heading('5.1. Evaluation metrics', 2)
    doc.add_paragraph(
        "The evaluation metrics used are the coefficient of determination R², the root mean square error (RMSE), "
        "and the mean absolute error (MAE), defined respectively by: R² = 1 - (Σ(yᵢ - ŷᵢ)² / Σ(yᵢ - ȳ)²), "
        "RMSE = √(1/N Σ(yᵢ - ŷᵢ)²), and MAE = 1/N Σ|yᵢ - ŷᵢ|, where yᵢ is the actual value, ŷᵢ the prediction, and N the number of samples."
    )
    
    doc.add_heading('5.2. Renewable production results (1h and 24h)', 2)
    doc.add_paragraph(f"Production 1h: R² = {prod_1h_r2:.3f}, MAE = {prod_1h_mae:.2f} Wh (Expected: >0.95)")
    try:
        doc.add_picture('trained_models/production_1h_pred_plot.jpg', width=Inches(6.0))
    except Exception as e:
        print("Missing production 1h images:", e)
        
    doc.add_paragraph(f"Production 24h: R² = {prod_24h_r2:.3f}, MAE = {prod_24h_mae:.2f} Wh")
    try:
        doc.add_picture('trained_models/production_24h_pred_plot.jpg', width=Inches(6.0))
    except Exception as e:
        print("Missing production 24h images:", e)
    
    doc.add_heading('5.3. Consumption results 1h', 2)
    doc.add_paragraph(f"Consumption 1h: R² = {cons_1h_r2:.3f}, MAE = {cons_1h_mae:.2f} Wh (Expected: >0.85)")
    try:
        doc.add_picture('trained_models/consumption_1h_pred_plot.jpg', width=Inches(6.0))
    except Exception as e:
        print("Missing consumption 1h images:", e)
    
    doc.add_heading('5.4. Comparison with baseline approaches', 2)
    doc.add_paragraph(f"Production 1h baselines:\n- LightGBM: R²={prod_1h_lgb:.3f}\n- XGBoost:  R²={prod_1h_xgb:.3f}\n\nConsumption 1h baselines:\n- LightGBM: R²={cons_1h_lgb:.3f}\n- XGBoost:  R²={cons_1h_xgb:.3f}")
    
    doc.add_heading('5.5. Error analysis and limitations', 2)
    doc.add_paragraph(
        f"The lower performance of the consumption model (R² = {cons_1h_r2:.3f}) compared to production (R² = {prod_1h_r2:.3f}) is explained "
        "by the stochastic nature of human behavior, which is difficult to model. Mahjoub et al. (2022) observed similar "
        "difficulties, with R² ranging from 0.70 to 0.97 depending on the horizon. The use of industrial schedules for "
        "24h consumption allows this limitation to be circumvented in degraded mode."
    )
    
    # 6. Peak Detection and Confidence Score
    doc.add_heading('6. Peak Detection and Confidence Score', 1)
    doc.add_heading('6.1. Detection of consumption and production peaks', 2)
    doc.add_paragraph("Peaks are defined as consumption above the 90th percentile (113.6 Wh).")
    doc.add_heading('6.2. Confidence score computation', 2)
    doc.add_paragraph("Score (%) = 100 * (1 - MAE_bin / max(|Prediction|, Floor)). Confidence is dynamically computed based on the expected validation MAE for the predicted decile.")
    doc.add_heading('6.3. Confidence levels', 2)
    doc.add_paragraph("High (>85%), Medium (70-85%), Low (<70%).")
    
    # 7. Industrialization
    doc.add_heading('7. Industrialization and Integration', 1)
    doc.add_heading('7.1. REST API exposure', 2)
    doc.add_paragraph("The models are served via a lightweight REST API using FastAPI on the Edge device. This exposes the predictions natively to the local network.")
    doc.add_heading('7.2. Retraining frequency and federated learning', 2)
    doc.add_paragraph("The models are automatically retrained daily using a walk-forward refit schedule. They actively participate in federated learning updates globally, updating their weights based on aggregated fleet intelligence.")
    doc.add_heading('7.3. Performance constraints (inference < 1s)', 2)
    doc.add_paragraph(
        "The experiments were conducted with LightGBM 4.1.0 on Python 3.11, on a server equipped with an Intel Xeon 8-core "
        "processor and 32 GB of RAM. Training of the 1h consumption model took 12 minutes with early stopping at 100 rounds. "
        "Time-series cross-validation was performed with an expanding window of 30 days."
    )
    doc.add_heading('7.4. Integration with digital twin and Rule Engine', 2)
    doc.add_paragraph("Forecasts are pushed directly into the digital twin, allowing the Rule Engine to proactively schedule storage and optimize self-consumption by dynamically controlling the batteries and smart appliances.")
    
    # 8. Conclusion
    doc.add_heading('8. Conclusion and Perspectives', 1)
    doc.add_heading('8.1. Summary of performances and contributions', 2)
    doc.add_paragraph("The integration of XGBoost for consumption and LightGBM for production successfully created a hybrid system satisfying Edge inference limits and global accuracy goals. The consumption model benefited heavily from logarithmic transformation of targets and tailored interaction features.")
    doc.add_heading('8.2. Identified limitations', 2)
    doc.add_paragraph("Predicting human-driven anomalous spikes remains an inherent limitation of pure autoregressive models. The Edge constraints also bound the depth of trees and sequence history we can feed the models.")
    doc.add_heading('8.3. Improvement paths', 2)
    doc.add_paragraph("Future work will explore deeper embeddings for human schedules and advanced meteorological ensembles for extreme weather.")
    
    # 9. References
    doc.add_heading('9. Bibliographic References', 1)
    doc.add_paragraph("[1] Yan et al. (2018) - Energy forecasting and smart grids.")
    doc.add_paragraph(
        "[2] Mahjoub et al. (2022) - Comparative study of LSTM, GRU, and Drop-GRU models for short-term energy forecasting."
    )
    doc.add_paragraph(
        "[3] Ke, G., et al. (2017). LightGBM: A Highly Efficient Gradient Boosting Decision Tree. Advances in Neural Information Processing Systems."
    )
    doc.add_paragraph(
        "[4] Hochreiter, S., & Schmidhuber, J. (1997). Long Short-Term Memory. Neural Computation."
    )
    doc.add_paragraph(
        "[5] McMahan, B., et al. (2017). Communication-Efficient Learning of Deep Networks from Decentralized Data (FedAvg)."
    )
    
    doc.add_paragraph(
        "\nNote on References: The work of Ke et al. (2017) introduced LightGBM with GOSS and EFB techniques, enabling a 60% reduction in training time compared to XGBoost. McMahan et al. (2017) proposed FedAvg for federated learning, used in H2S for continuous model improvement without centralizing data."
    )

    doc.save(output_path)
    print(f"Updated document saved to {output_path}")

if __name__ == '__main__':
    rewrite_docx(
        '/Users/navaneethakrishnan/NK/projects/H2S/H2S/Modele_AI/H2S_AI_Modeling_Report.docx', 
        '/Users/navaneethakrishnan/NK/projects/H2S/H2S/Modele_AI/H2S_AI_Modeling_Report.docx'
    )
