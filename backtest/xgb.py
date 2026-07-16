import argparse
from pathlib import Path
import bottleneck as bn
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
import xgboost as xgb

EPS = 1e-12
SPAN = (5, 15, 60, 240)


def mv_sum(x, w):
    return bn.move_sum(x, window=w, axis=0, min_count=w)


def mv_mean(x, w):
    return bn.move_mean(x, window=w, axis=0, min_count=w)


def mv_var(x, w):
    return bn.move_var(x, window=w, axis=0, min_count=w, ddof=0)


def mv_max(x, w):
    return bn.move_max(x, window=w, axis=0, min_count=w)


def mv_min(x, w):
    return bn.move_min(x, window=w, axis=0, min_count=w)


def rv_demeaned(x, w):
    return np.sqrt(np.maximum(mv_var(x, w), 0.0))


def safe_log(x):
    x = np.asarray(x)
    y = np.full(x.shape, np.nan, dtype=np.result_type(x, float))
    ok = np.isfinite(x) & (x > 0)
    np.log(x, out=y, where=ok)
    return y


def log1p_pos(x):
    x = np.asarray(x)
    y = np.full(x.shape, np.nan, dtype=np.result_type(x, float))
    ok = np.isfinite(x) & (x > 0)
    np.log1p(x, out=y, where=ok)
    return y


def make_features(Rm, Om, Hm, Lm, Vm, Tm, Vbm, I):
    M, N = Rm.shape
    feats, names = [], []

    def add(a, name):
        feats.append(np.asarray(a))
        names.append(name)

    def lag(a, k):
        if k < 1:
            raise ValueError("lag k must be >= 1.")
        out = np.full((M, N), np.nan, dtype=np.result_type(a, float))
        out[k:] = a[:-k]
        return out

    for w in SPAN:
        log_o = safe_log(lag(Om, w - 1))
        log_h = safe_log(mv_max(Hm, w))
        log_l = safe_log(mv_min(Lm, w))
        add(log_l - log_o, f"log_lo_{w}")
        add(log_h - log_l, f"log_hi_{w}")

    rv = {w: rv_demeaned(Rm, w) for w in SPAN}
    for w in SPAN:
        add(rv[w], f"rv_{w}")

    Rm3 = Rm * Rm * Rm
    rs, abs_rs = {}, {}
    for w in (1, *SPAN):
        rs_w = mv_sum(Rm, w)
        abs_rs_w = np.abs(rs_w)
        rs[w] = rs_w
        abs_rs[w] = abs_rs_w
        add(rs_w, f"rs_{w}")
        add(abs_rs_w, f"abs_rs_{w}")

    for w in SPAN:
        rv_eps = rv[w] + EPS
        add(rs[w] / rv_eps, f"rs_over_rv_{w}")
        add(abs_rs[w] / rv_eps, f"abs_rs_over_rv_{w}")
        add(mv_mean(Rm3, w) / (rv_eps ** 3), f"skew_{w}")

    for s, l in ((5, 15), (15, 60), (60, 240)):
        add(rv[s] / (rv[l] + EPS), f"rv_ratio_{s}_{l}")
        add(rv[s] - rv[l], f"rv_diff_{s}_{l}")

    lv = log1p_pos(Vm)
    lv_ma_5 = None
    lv_ma_60 = None
    for w in SPAN:
        ma = mv_mean(lv, w)
        sur = lv - ma
        add(ma, f"log_v_ma_{w}")
        add(sur, f"log_v_sur_{w}")
        add(sur * rv[w], f"log_v_sur_x_rv_{w}")
        if w == 5:
            lv_ma_5 = ma
        elif w == 60:
            lv_ma_60 = ma
    add(lv_ma_5 - lv_ma_60, "log_v_trend_5_60")

    lvb = log1p_pos(Vbm)
    add(lvb, "log_buy_v")
    for w in SPAN:
        add(mv_mean(lvb, w), f"log_buy_v_ma_{w}")

    buy_share = np.full((M, N), np.nan, dtype=np.result_type(Vbm, float))
    ok_share = np.isfinite(Vbm) & np.isfinite(Vm) & (Vbm >= 0) & (Vm > 0)
    np.divide(Vbm, Vm, out=buy_share, where=ok_share)
    add(buy_share, "buy_volume_share")

    dpt = np.full((M, N), np.nan, dtype=np.result_type(Vm, float))
    ok_dpt = np.isfinite(Vm) & np.isfinite(Tm) & (Vm >= 0) & (Tm > 0)
    np.divide(Vm, Tm, out=dpt, where=ok_dpt)
    add(log1p_pos(dpt), "log_dollar_per_trade")

    I = int(I)
    if I < 2:
        raise ValueError("I must be >= 2.")
    phase = (np.arange(M) % I) / (I - 1)
    ang1 = 2.0 * np.pi * phase
    ang2 = 4.0 * np.pi * phase
    for fn, name in ((np.sin, "sin"), (np.cos, "cos")):
        add(np.broadcast_to(fn(ang1)[:, None], (M, N)), f"{name}_tod")
        add(np.broadcast_to(fn(ang2)[:, None], (M, N)), f"{name}2_tod")

    return np.stack(feats, axis=-1), names


def make_target_future_vol(Rm, horizon=30, skip=1):
    if skip < 1:
        raise ValueError("skip must be >= 1.")
    Rm = np.asarray(Rm, dtype=float)
    m2_fwd = bn.move_mean((Rm * Rm)[::-1], window=horizon, axis=0, min_count=horizon)[::-1]
    rv_end = np.sqrt(np.maximum(m2_fwd, 0.0))
    y = np.full_like(rv_end, np.nan)
    y[:-skip] = rv_end[skip:]
    return y


def rmspe(y_true, y_pred, eps=EPS):
    y_true = np.asarray(y_true, dtype=np.float64)
    y_pred = np.asarray(y_pred, dtype=np.float64)
    return float(np.sqrt(np.mean(((y_pred - y_true) / np.maximum(np.abs(y_true), eps)) ** 2)))


def flatten_xy(Xm, ym, minutes_sel, return_minute_offsets=False):
    X = Xm[minutes_sel]
    y = ym[minutes_sel]
    ok = np.isfinite(y) & (np.abs(y) > 1e-4)
    X2 = X[ok]
    y2 = y[ok]
    if not return_minute_offsets:
        return X2, y2
    counts = ok.sum(axis=1).astype(np.int64)
    minute_offsets = np.empty(len(counts) + 1, dtype=np.int64)
    minute_offsets[0] = 0
    np.cumsum(counts, out=minute_offsets[1:])
    return X2, y2, minute_offsets


def fit_linear_ols(X, y):
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    X_design = np.concatenate((np.ones((X.shape[0], 1), dtype=np.float64), X), axis=1)
    beta, *_ = np.linalg.lstsq(X_design, y, rcond=None)
    return beta


def predict_linear_ols(X, beta):
    X = np.asarray(X, dtype=np.float64)
    X_design = np.concatenate((np.ones((X.shape[0], 1), dtype=np.float64), X), axis=1)
    return X_design @ beta


def time_cv_ranges(n, n_splits=5, gap=1000):
    val_size = (n - n_splits * gap) // (n_splits + 1)
    if val_size < 1:
        raise ValueError("Not enough minutes for requested n_splits and gap.")
    init_train = n - n_splits * (val_size + gap)
    if init_train < 1:
        raise ValueError("Initial train window is empty.")
    for i in range(n_splits):
        tr_end = init_train + i * (val_size + gap)
        va_start = tr_end + gap
        va_end = va_start + val_size
        if va_end > n:
            break
        yield slice(0, tr_end), slice(va_start, va_end)


def sampled_clip_bounds(X, q_low, q_high, sample_rows, rng):
    if sample_rows < 1:
        raise ValueError("sample_rows must be >= 1.")
    if X.shape[0] > sample_rows:
        idx = rng.choice(X.shape[0], size=sample_rows, replace=False)
        Xq = X[idx]
    else:
        Xq = X
    lo = np.nanquantile(Xq, q_low, axis=0)
    hi = np.nanquantile(Xq, q_high, axis=0)
    return lo, hi


def train_xgb_optuna_timeseries(
    X_tr,
    y_tr,
    minute_offsets,
    n_trials=96,
    n_splits=5,
    gap=1000,
    n_gpus=8,
    seed=42,
    optuna_jobs=24,
    clip_q_low=0.01,
    clip_q_high=0.99,
    clip_sample_rows=1000000,
):
    minute_offsets = np.asarray(minute_offsets, dtype=np.int64)
    if n_gpus < 1:
        raise ValueError("n_gpus must be >= 1.")

    rng = np.random.default_rng(seed)
    folds = []
    for tr_m_sl, va_m_sl in time_cv_ranges(len(minute_offsets) - 1, n_splits=n_splits, gap=gap):
        tr_rows = slice(int(minute_offsets[tr_m_sl.start]), int(minute_offsets[tr_m_sl.stop]))
        va_rows = slice(int(minute_offsets[va_m_sl.start]), int(minute_offsets[va_m_sl.stop]))
        if (tr_rows.stop > tr_rows.start) and (va_rows.stop > va_rows.start):
            Xtr = X_tr[tr_rows]
            Xva = X_tr[va_rows]
            lo, hi = sampled_clip_bounds(Xtr, clip_q_low, clip_q_high, clip_sample_rows, rng)
            folds.append((
                np.clip(Xtr, lo[None, :], hi[None, :]),
                y_tr[tr_rows],
                np.clip(Xva, lo[None, :], hi[None, :]),
                y_tr[va_rows],
            ))
    if not folds:
        raise ValueError("No valid folds after filtering.")

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )

    def obj(trial):
        params = dict(
            n_estimators=2000,
            learning_rate=trial.suggest_float("lr", 1e-3, 0.1, log=True),
            max_depth=trial.suggest_int("max_depth", 3, 10),
            min_child_weight=trial.suggest_float("min_child_weight", 1, 7),
            subsample=trial.suggest_float("subsample", 0.3, 1.0),
            colsample_bytree=trial.suggest_float("colsample_bytree", 0.3, 1.0),
            reg_alpha=trial.suggest_float("reg_alpha", 1e-5, 10.0, log=True),
            reg_lambda=trial.suggest_float("reg_lambda", 1e-5, 10.0, log=True),
            gamma=trial.suggest_float("gamma", 1e-5, 1.0, log=True),
            objective="reg:squarederror",
            tree_method="hist",
            device=f"cuda:{trial.number % n_gpus}",
            n_jobs=1,
            random_state=seed,
            verbosity=0,
        )
        scores = []
        for Xtr, ytr, Xva, yva in folds:
            m = xgb.XGBRegressor(**params, early_stopping_rounds=50, eval_metric=rmspe)
            m.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
            scores.append(rmspe(yva, m.predict(Xva)))
        return float(np.mean(scores))

    study.optimize(obj, n_trials=n_trials, n_jobs=optuna_jobs)

    best = study.best_params
    best_full = dict(
        n_estimators=2000,
        learning_rate=best["lr"],
        max_depth=best["max_depth"],
        min_child_weight=best["min_child_weight"],
        subsample=best["subsample"],
        colsample_bytree=best["colsample_bytree"],
        reg_alpha=best["reg_alpha"],
        reg_lambda=best["reg_lambda"],
        gamma=best["gamma"],
        objective="reg:squarederror",
        tree_method="hist",
        random_state=seed,
        verbosity=0,
    )
    return study, best_full


def plot_xgb_feature_importance(model, feature_names, topk=40, title="XGB feature importance"):
    imp = np.asarray(model.feature_importances_, dtype=np.float64)
    idx = np.argsort(imp)[::-1][:topk]
    plt.figure(figsize=(10, 10))
    plt.barh(np.asarray(feature_names, dtype=object)[idx][::-1], imp[idx][::-1])
    plt.title(title)
    plt.tight_layout()
    plt.show()


def _symbol_dirs(root):
    return sorted(p for p in root.iterdir() if p.is_dir() and any(p.glob("*.feather")))


def _date_key(p):
    return p.stem[:8]


def load_ohlcv_stack(root):
    dirs = _symbol_dirs(root)
    if not dirs:
        raise RuntimeError("No symbol folders with .feather files found.")

    sym_files = {}
    for d in dirs:
        day_map = {}
        for f in sorted(d.glob("*.feather")):
            k = _date_key(f)
            if k in day_map:
                raise ValueError(f"Duplicate day key {k} in {d}")
            day_map[k] = f
        sym_files[d.name] = day_map

    common_dates = sorted(set.intersection(*(set(m.keys()) for m in sym_files.values())))
    if not common_dates:
        raise RuntimeError("No common dates across symbol folders.")

    need = ["open", "high", "low", "close", "volume", "num_trades", "tbqv"]
    I = int(min(
        len(pd.read_feather(m[day], columns=["close"]))
        for m in sym_files.values()
        for day in common_dates
    ))
    if I <= 0:
        raise RuntimeError("Empty intraday data detected.")

    T, N = len(common_dates), len(dirs)
    shape = (T, I, N)
    O = np.empty(shape, dtype=float)
    H = np.empty(shape, dtype=float)
    L = np.empty(shape, dtype=float)
    C = np.empty(shape, dtype=float)
    V = np.empty(shape, dtype=float)
    N_TRADES = np.empty(shape, dtype=float)
    V_BUY = np.empty(shape, dtype=float)

    for n, d in enumerate(dirs):
        fmap = sym_files[d.name]
        for t, day in enumerate(common_dates):
            df = pd.read_feather(fmap[day], columns=need).iloc[:I]
            vals = df[need].to_numpy(dtype=float, copy=False).T
            O[t, :, n], H[t, :, n], L[t, :, n], C[t, :, n], V[t, :, n], N_TRADES[t, :, n], V_BUY[t, :, n] = vals

    log_c = np.log(np.clip(C, EPS, None))
    R = np.diff(log_c, axis=1, prepend=log_c[:, :1, :])
    return R, O, H, L, C, V, N_TRADES, V_BUY, common_dates, [d.name for d in dirs]


def final_tail_split_rows(minute_offsets, gap, val_tail):
    minute_offsets = np.asarray(minute_offsets, dtype=np.int64)
    n_minutes = len(minute_offsets) - 1
    if n_minutes < 2:
        raise ValueError("Need at least 2 train minutes for final fit/eval split.")
    val_tail = min(max(1, int(val_tail)), n_minutes - 1)
    eval_start = n_minutes - val_tail
    train_end = eval_start - min(int(gap), max(eval_start - 1, 0))
    rows_fit = slice(int(minute_offsets[0]), int(minute_offsets[train_end]))
    rows_es = slice(int(minute_offsets[eval_start]), int(minute_offsets[n_minutes]))
    if (rows_fit.stop <= rows_fit.start) or (rows_es.stop <= rows_es.start):
        raise ValueError("Final fit/eval split has empty rows. Reduce gap or val_tail.")
    return rows_fit, rows_es


def main(root=Path("."), baseline_only=False, baseline_rv_windows=SPAN):
    R, O, H, L, _, V, N_TRADES, V_BUY, dates, symbols = load_ohlcv_stack(root)
    print("Loaded:", len(dates), "days,", len(symbols), "symbols, I=", R.shape[1])

    T, I, N = R.shape
    M = T * I
    Rm, Om, Hm, Lm, Vm, Tm, Vbm = [
        a.reshape(M, N) for a in (R, O, H, L, V, N_TRADES, V_BUY)
    ]

    y = make_target_future_vol(Rm, horizon=30, skip=1)

    if baseline_only:
        rv_windows = tuple(int(w) for w in baseline_rv_windows)
        X = np.stack([rv_demeaned(Rm, w) for w in rv_windows], axis=-1)
        split = int(0.8 * M)
        tr_minutes = np.arange(split, dtype=np.int64)
        te_minutes = np.arange(split, M, dtype=np.int64)
        X_tr_3d, y_tr_2d = X[tr_minutes], y[tr_minutes]
        X_te_3d, y_te_2d = X[te_minutes], y[te_minutes]

        tr_ok = np.isfinite(y_tr_2d) & (np.abs(y_tr_2d) > 1e-4) & np.isfinite(X_tr_3d).all(axis=2)
        te_ok = np.isfinite(y_te_2d) & (np.abs(y_te_2d) > 1e-4) & np.isfinite(X_te_3d).all(axis=2)

        X_tr_flat, y_tr_flat = X_tr_3d[tr_ok], y_tr_2d[tr_ok]
        X_te_flat, y_te_flat = X_te_3d[te_ok], y_te_2d[te_ok]

        beta = fit_linear_ols(X_tr_flat, y_tr_flat)
        yhat_flat = predict_linear_ols(X_te_flat, beta)
        print("OOS_test_RMSPE:", rmspe(y_te_flat, yhat_flat))

        yhat_te_2d = np.full_like(y_te_2d, np.nan, dtype=np.float64)
        yhat_te_2d[te_ok] = yhat_flat
        for n, sym in enumerate(symbols):
            sym_ok = te_ok[:, n]
            print(f"OOS_test_RMSPE_{sym}:", rmspe(y_te_2d[sym_ok, n], yhat_te_2d[sym_ok, n]))
        return

    X, fnames = make_features(Rm, Om, Hm, Lm, Vm, Tm, Vbm, I=I)

    split = int(0.8 * M)
    tr_minutes = np.arange(split, dtype=np.int64)
    te_minutes = np.arange(split, M, dtype=np.int64)

    X_tr_3d, y_tr_3d = X[tr_minutes], y[tr_minutes]
    X_te_3d, y_te_3d = X[te_minutes], y[te_minutes]

    tr_local = np.arange(X_tr_3d.shape[0], dtype=np.int64)
    X_tr, y_tr, tr_offsets = flatten_xy(X_tr_3d, y_tr_3d, tr_local, return_minute_offsets=True)
    X_te, y_te = flatten_xy(X_te_3d, y_te_3d, np.arange(X_te_3d.shape[0], dtype=np.int64))

    study, best_full = train_xgb_optuna_timeseries(
        X_tr,
        y_tr,
        tr_offsets,
        n_trials=96,
        n_splits=5,
        gap=1000,
        n_gpus=1,
        seed=42,
        optuna_jobs=3,
    )

    rows_fit, rows_es = final_tail_split_rows(tr_offsets, gap=1000, val_tail=max(1, len(tr_minutes) // 10))
    lo, hi = sampled_clip_bounds(
        X_tr[rows_fit],
        q_low=0.01,
        q_high=0.99,
        sample_rows=1000000,
        rng=np.random.default_rng(42),
    )
    X_fit = np.clip(X_tr[rows_fit], lo[None, :], hi[None, :])
    y_fit = y_tr[rows_fit]
    X_es = np.clip(X_tr[rows_es], lo[None, :], hi[None, :])
    y_es = y_tr[rows_es]
    X_te = np.clip(X_te, lo[None, :], hi[None, :])

    xgbm = xgb.XGBRegressor(
        **best_full,
        device="cuda:0",
        n_jobs=3,
        early_stopping_rounds=50,
        eval_metric=rmspe,
    )
    xgbm.fit(X_fit, y_fit, eval_set=[(X_es, y_es)], verbose=False)

    yhat = xgbm.predict(X_te)
    print("OOS_test_RMSPE:", rmspe(y_te, yhat))
    print("optuna_best_params:", study.best_params)
    print("optuna_best_value(mean_CV_RMSPE):", study.best_value)

    plot_xgb_feature_importance(
        xgbm,
        fnames,
        topk=56,
        title=f"Feature importance ({getattr(xgbm, 'importance_type', 'gain')})",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--baseline-rv-windows", type=int, nargs="+", default=list(SPAN))
    args = parser.parse_args()
    main(
        root=args.root,
        baseline_only=args.baseline_only,
        baseline_rv_windows=args.baseline_rv_windows,
    )
