import joblib
bundle = joblib.load('trained_models/production_24h.pkl')
print(bundle.keys())
print(bundle['models'].keys())
m = bundle['models']['production_wh']
print(hasattr(m, 'evals_result_'))
