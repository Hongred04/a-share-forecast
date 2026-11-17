"""自进化机制的可改进性验证（离线，用已落库的 day_log / sig_log）。

评测口径
    跳空模型 : 逐日 walk-forward，只用当日之前的数据拟合，预测当日跳空
               指标 RMSE / MAE / 方向命中
    日内模型 : 同上，预测当日日内涨跌方向（yi）
               指标 命中率 / logloss
所有变体都在同一段时间上比较，避免口径不一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import analytics as A
from core import engine as E

WIN = 700


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    with E.db() as c:
        d = pd.DataFrame([dict(r) for r in c.execute(
            "SELECT asof,ov,gap,intra,c2c FROM day_log ORDER BY asof")])
        s = pd.DataFrame([dict(r) for r in c.execute(
            "SELECT asof,key,score,yi FROM sig_log WHERE live=0")])
    pv = s.pivot_table(index="asof", columns="key", values="score", aggfunc="last").sort_index()
    yi = s.groupby("asof")["yi"].first().reindex(pv.index)
    pv = pv.reindex(columns=A.HIST_EXPERTS).fillna(0.0)
    return d.set_index("asof"), pv.join(yi.rename("yi"))


def ols(X, y, w=None, l2=1e-8):
    n, k = X.shape
    Xa = np.column_stack([X, np.ones(n)])
    if w is not None:
        sw = np.sqrt(w)[:, None]
        Xa, y = Xa * sw, y * np.sqrt(w)
    reg = np.eye(k + 1) * l2
    reg[-1, -1] = 0.0
    beta = np.linalg.solve(Xa.T @ Xa + reg, Xa.T @ y)
    resid = y - Xa @ beta
    sd = float(np.std(resid)) if len(resid) > 3 else 1.0
    return beta[:-1], float(beta[-1]), max(sd, 1e-3)


# ----------------------------------------------------------------------------------
# 跳空模型变体
# ----------------------------------------------------------------------------------
def gap_walk(d: pd.DataFrame, mode: str, window: int = WIN, shrink: float = 0.65) -> dict:
    ov, gap = d["ov"].to_numpy(), d["gap"].to_numpy()
    n = len(d)
    pred = np.full(n, np.nan)
    for i in range(120, n):
        lo = max(0, i - window)
        x, y = ov[lo:i], gap[lo:i]
        w = None
        if mode == "ewma":
            age = np.arange(len(y))[::-1]
            w = 0.5 ** (age / 250.0)
        if mode == "kink":
            # 极端外盘（|ov|>1.5）单独斜率，其余一条
            Xk = np.column_stack([x, np.abs(x) * (np.abs(x) > 1.5)])
            bk, ak, _ = ols(Xk, y, w)
            b = bk[0] + bk[1] * (abs(ov[i]) > 1.5)
            a = ak
        elif mode == "ensemble":
            lo2 = max(0, i - 1200)
            b1, a1, _ = ols(ov[lo2:i, None], gap[lo2:i])
            b2, a2, _ = ols(ov[lo:i, None], gap[lo:i])
            b = 0.5 * (b1[0] + b2[0])
            a = 0.5 * (a1 + a2)
        else:
            b, a, _ = ols(x[:, None], y, w)
            b = b[0]
        b = shrink * b + (1 - shrink) * 0.226
        a = float(np.mean(y - b * x))
        pred[i] = a + b * ov[i]
    m = ~np.isnan(pred)
    err = pred[m] - gap[m]
    return {"rmse": round(float(np.sqrt(np.mean(err ** 2))), 4),
            "mae": round(float(np.mean(np.abs(err))), 4),
            "hit": round(float(np.mean((pred[m] > 0) == (gap[m] > 0))), 4),
            "n": int(m.sum())}


# ----------------------------------------------------------------------------------
# 日内模型变体
# ----------------------------------------------------------------------------------
def intra_walk(pv: pd.DataFrame, mode: str = "base", window: int = WIN,
               l2: float = 6.0, clip: float = 0.6) -> dict:
    cols = A.HIST_EXPERTS
    X = pv[cols].to_numpy(float)
    y = pv["yi"].to_numpy(float)
    n = len(y)
    pred = np.full(n, np.nan)
    base = float(y[:120].mean()) if n > 120 else 0.5
    const = np.full(n, base)

    for i in range(120, n):
        lo = max(0, i - window)
        if mode == "const":
            pred[i] = base
            continue
        # 置信门控：滚动 120 日 logloss 不如常数基准，就闭嘴
        if mode == "gated" and i > 240:
            hist = slice(max(0, i - 120), i)
            ll_m = -np.mean(y[hist] * np.log(np.clip(pred[hist], 1e-6, 1)) +
                            (1 - y[hist]) * np.log(np.clip(1 - pred[hist], 1e-6, 1)))
            ll_c = -np.mean(y[hist] * np.log(const[hist]) +
                            (1 - y[hist]) * np.log(1 - const[hist]))
            if ll_m >= ll_c:
                pred[i] = base
                continue
        w, b0 = A.fit_logistic_multi(X[lo:i], y[lo:i], l2=l2)
        w = np.clip(w, -clip, clip)
        b0 = float(np.clip(b0, -0.55, 0.55))
        pred[i] = 1 / (1 + np.exp(-(X[i] @ w + b0)))
    m = ~np.isnan(pred)
    p = np.clip(pred[m], 1e-6, 1 - 1e-6)
    yy = y[m]
    ll = -np.mean(yy * np.log(p) + (1 - yy) * np.log(1 - p))
    return {"hit": round(float(np.mean((p > 0.5) == (yy > 0))), 4),
            "logloss": round(float(ll), 4),
            "up_rate": round(float(yy.mean()), 4),
            "n": int(m.sum())}


def main() -> None:
    d, pv = load()
    print(f"样本 {len(d)} 日  {d.index[0]} ~ {d.index[-1]}")
    print(f"跳空均值 {d.gap.mean():+.3f}%  日内均值 {d.intra.mean():+.3f}%  "
          f"日内上涨率 {pv.yi.mean():.3f}\n")

    print("--- 跳空模型 ---")
    for mode, kw, lab in [
        ("base", {}, "基线 w700 shrink.65"),
        ("base", {"window": 250}, "短窗 w250"),
        ("base", {"window": 1200}, "长窗 w1200"),
        ("ewma", {"window": 700}, "指数加权 hl250"),
        ("ensemble", {}, "长短窗集成"),
        ("kink", {}, "极端外盘单独斜率"),
        ("base", {"shrink": 0.4}, "收缩 .40"),
        ("base", {"shrink": 0.9}, "收缩 .90"),
    ]:
        r = gap_walk(d, mode, **kw)
        print(f"  {lab:22s} RMSE {r['rmse']:.3f}  MAE {r['mae']:.3f}  命中 {r['hit']:.3f}")

    print("\n--- 日内模型 ---")
    for mode, kw, lab in [
        ("base", {}, "基线 l2=6 w700"),
        ("const", {}, "常数（只用基准率）"),
        ("base", {"l2": 12}, "强正则 l2=12"),
        ("base", {"l2": 3}, "弱正则 l2=3"),
        ("base", {"window": 300}, "短窗 w300"),
        ("gated", {}, "置信门控"),
    ]:
        r = intra_walk(pv, mode, **kw)
        print(f"  {lab:22s} 命中 {r['hit']:.3f}  logloss {r['logloss']:.4f}  (基准 {r['up_rate']:.3f})")


if __name__ == "__main__":
    main()
