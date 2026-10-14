"""Baseline models for the comparison of report §5.4.

* Persistence / seasonal-naive references
* XGBoost on the *same* engineered features as LightGBM
* LSTM and GRU (PyTorch) on sequences of the last ``L`` standardised feature vectors
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import StandardScaler
from torch import nn

from . import config as C

torch.set_num_threads(8)


def xgboost_baseline(X_tr, y_tr, X_va, y_va, X_te) -> tuple[np.ndarray, float]:
    import xgboost as xgb

    t0 = time.time()
    m = xgb.XGBRegressor(n_estimators=3000, learning_rate=0.03, max_depth=6, subsample=0.8, colsample_bytree=0.8,
                         min_child_weight=5, reg_lambda=1.0, tree_method="hist", early_stopping_rounds=100,
                         random_state=C.RANDOM_STATE, n_jobs=-1)
    m.fit(X_tr, y_tr, eval_set=[(X_va, y_va)], verbose=False)
    return m.predict(X_te), time.time() - t0


class _RNN(nn.Module):
    def __init__(self, n_in: int, cell: str, hidden: int = 64, dropout: float = 0.2):
        super().__init__()
        rnn = nn.LSTM if cell == "lstm" else nn.GRU
        self.rnn = rnn(n_in, hidden, num_layers=2, batch_first=True, dropout=dropout)
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(hidden + n_in, 64), nn.ReLU(), nn.Linear(64, 1))

    def forward(self, x):
        out, _ = self.rnn(x)
        return self.head(torch.cat([out[:, -1], x[:, -1]], dim=1)).squeeze(-1)


def make_sequences(Xs: np.ndarray, positions: np.ndarray, L: int, stride: int) -> np.ndarray:
    offs = np.arange(L - 1, -1, -1) * stride
    pos = positions[:, None] - offs[None, :]
    pos = np.clip(pos, 0, len(Xs) - 1)
    return Xs[pos]


def rnn_baseline(X: pd.DataFrame, y: np.ndarray, pos_tr, pos_va, pos_te, cell: str,
                 L: int = 24, stride: int = 1, epochs: int = 60, patience: int = 8) -> tuple[np.ndarray, float]:
    """Train an LSTM/GRU on Z-score-normalised sequences (StandardScaler fitted on train only)."""
    t0 = time.time()
    torch.manual_seed(C.RANDOM_STATE)
    np.random.seed(C.RANDOM_STATE)
    sc = StandardScaler().fit(X.iloc[pos_tr])
    Xs = np.nan_to_num(sc.transform(X), nan=0.0).astype(np.float32)
    ys = StandardScaler().fit(y[pos_tr].reshape(-1, 1))
    yn = ys.transform(y.reshape(-1, 1)).ravel().astype(np.float32)

    def tensors(p):
        return torch.from_numpy(make_sequences(Xs, p, L, stride)), torch.from_numpy(yn[p])

    xtr, ytr = tensors(pos_tr)
    xva, yva = tensors(pos_va)
    xte, _ = tensors(pos_te)
    model = _RNN(Xs.shape[1], cell)
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    loss_fn = nn.MSELoss()
    best, best_state, bad = np.inf, None, 0
    for _ in range(epochs):
        model.train()
        perm = torch.randperm(len(xtr))
        for i in range(0, len(perm), 256):
            b = perm[i:i + 256]
            opt.zero_grad()
            loss = loss_fn(model(xtr[b]), ytr[b])
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        model.eval()
        with torch.no_grad():
            v = loss_fn(model(xva), yva).item()
        if v < best - 1e-4:
            best, bad = v, 0
            best_state = {k: t.clone() for k, t in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        pred = model(xte).numpy()
    return ys.inverse_transform(pred.reshape(-1, 1)).ravel(), time.time() - t0
