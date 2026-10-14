import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path

def plot_predictions(csv_path: Path):
    df = pd.read_csv(csv_path, parse_dates=['time'])
    df = df.set_index('time')
    
    # Extract the target name (the column name ending with _true)
    true_col = [c for c in df.columns if c.endswith('_true')]
    if not true_col:
        print(f"No _true column found in {csv_path.name}")
        return
        
    target_name = true_col[0].replace('_true', '')
    pred_col = f"{target_name}_pred"
    
    if pred_col not in df.columns:
        print(f"No {pred_col} column found in {csv_path.name}")
        return
        
    plt.figure(figsize=(10, 5))
    
    # Plot a subset if it's too long to be readable, say first 14 days
    df = df.head(14 * 24)
    
    plt.plot(df.index, df[true_col[0]], label='Actual (True)', alpha=0.7)
    plt.plot(df.index, df[pred_col], label='Predicted', alpha=0.7, linestyle='--')
    
    plt.title(f"Predictions vs Actuals: {csv_path.stem}")
    plt.xlabel("Time")
    plt.ylabel(target_name)
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    
    out_path = csv_path.parent.parent / f"{csv_path.stem}_pred_plot.png"
    plt.savefig(out_path, facecolor='white', transparent=False)
    plt.close()
    print(f"Saved {out_path}")

if __name__ == '__main__':
    for csv in Path("trained_models/predictions").glob("*.csv"):
        plot_predictions(csv)
