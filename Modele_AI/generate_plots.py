import joblib
import matplotlib.pyplot as plt
from pathlib import Path

def plot_curves(pkl_path: str, out_path: str):
    m = joblib.load(pkl_path)
    models = m['models']
    
    n_targets = len(models)
    fig, axes = plt.subplots(1, n_targets, figsize=(5 * n_targets, 4), squeeze=False)
    
    for i, (target, model) in enumerate(models.items()):
        ax = axes[0, i]
        
        # If wrapped in TransformedTargetRegressor, extract the base regressor
        if hasattr(model, 'regressor_'):
            model = model.regressor_
            
        evals = None
        if hasattr(model, 'evals_result_') and model.evals_result_:
            evals = model.evals_result_
        elif hasattr(model, 'evals_result') and callable(model.evals_result):
            try:
                evals = model.evals_result()
            except Exception:
                evals = None
            
        if evals:
            # XGBoost format: {'validation_0': {'rmse': [...]}}
            # LightGBM format: {'training': {'l2': [...]}, 'valid_1': {'l2': [...]}}
            train_key = list(evals.keys())[0] if len(evals) > 0 else None
            valid_key = list(evals.keys())[1] if len(evals) > 1 else None
            
            if train_key:
                metric = list(evals[train_key].keys())[0]
                ax.plot(evals[train_key][metric], label='Train')
                if valid_key and metric in evals[valid_key]:
                    ax.plot(evals[valid_key][metric], label='Validation')
                
                ax.set_title(f"Loss Curve: {target}")
                ax.set_xlabel("Epochs / Trees")
                ax.set_ylabel(metric.upper() + " Loss")
                ax.legend()
                ax.grid(True)
            else:
                ax.set_title(f"{target} (No eval results)")
        else:
            ax.set_title(f"{target} (No eval results)")
            
    plt.tight_layout()
    plt.savefig(out_path, facecolor='white', transparent=False)
    plt.close()
    print(f"Saved {out_path}")

if __name__ == '__main__':
    for pkl in Path("trained_models").glob("*.pkl"):
        plot_curves(str(pkl), str(pkl).replace(".pkl", "_curve.png"))
