"""模型层：上下文构建、跳空/日内两段模型、概率校准与不确定性标定。

所有参数都由历史样本滚动估计并写入 meta 表，没有硬编码的拟合结果。

"""
from __future__ import annotations

from . import analytics as A
from . import config as C
import datetime as dt
import numpy as np
import pandas as pd
from .store import (DEFAULT_GAP, db, gap_model, intra_model, load_learner, meta_get, meta_set, save_learner)
from .market import (compute_breadth, compute_capital, news_sentiment)

def bench_row_at(bench: pd.DataFrame, asof: pd.Timestamp, live: bool) -> dict:
    if bench is None or bench.empty:
        return {}
    i = len(bench) - 1 if live else A.at_index(bench, asof)
    if i < 0:
        return {}
    last = bench.iloc[i]
    keep = ["r1", "r3", "r5", "r10", "r20", "dev5", "dev10", "dev20", "dev60",
            "rsi6", "rsi14", "macd", "atr_pct", "atr_rank", "vol20", "vol_ratio_amt",
            "pos20", "consec", "close"]
    return {k: float(last[k]) for k in keep if k in last.index and pd.notna(last[k])}

def panel_breadth_at(mkt: dict, asof: pd.Timestamp, live: bool) -> dict | None:
    panel = mkt.get("panel")
    if panel is None or panel.empty:
        return None
    sub = panel.tail(1) if live else panel[panel.index <= asof].tail(1)
    if sub.empty:
        return None
    row = sub.iloc[0]
    vals = [float(v) for v in row.to_numpy() if pd.notna(v)]
    if not vals:
        return None
    small_keys = [k for k in ("中证1000", "中证500", "创业板指", "科创50") if k in row]
    large_keys = [k for k in ("上证指数", "沪深300") if k in row]
    small = float(np.nanmean([row[k] for k in small_keys])) if small_keys else 0.0
    large = float(np.nanmean([row[k] for k in large_keys])) if large_keys else 0.0
    return {"up_share": float(np.mean([v > 0 for v in vals])),
            "median": float(np.median(vals)), "spread": small - large, "n": len(vals)}

def sector_stat_at(frames: dict[str, pd.DataFrame], asof: pd.Timestamp,
                   live: bool) -> dict | None:
    r1s, r5s = [], []
    for df in frames.values():
        if df is None or df.empty:
            continue
        i = len(df) - 1 if live else A.at_index(df, asof)
        if i < 8:
            continue
        v1, v5 = df["r1"].iloc[i], df["r5"].iloc[i]
        if pd.notna(v1):
            r1s.append(float(v1))
        if pd.notna(v5):
            r5s.append(float(v5))
    if len(r1s) < 8:
        return None
    arr = np.array(r1s)
    return {"up_share": float((arr > 0).mean()), "dispersion": float(arr.std()),
            "mean": float(arr.mean()),
            "leader_mom": float(np.percentile(r5s, 90)) if r5s else 0.0, "n": len(arr)}

def overseas_for(mkt: dict, asof: pd.Timestamp, live: bool) -> dict:
    out = {}
    for name, df in mkt["ov_raw"].items():
        if df is None or df.empty:
            continue
        j = len(df) - 1 if live else A.at_index(df, asof)
        if j < 0:
            continue
        out[name] = float(df["pct"].iloc[j])
    return out

def ov_weighted(ov: dict) -> float:
    """隔夜外盘加权涨跌幅（%）。"""
    if not ov:
        return 0.0
    tot = val = 0.0
    for name, w in C.OVERSEAS_CHAIN.items():
        v = ov.get(name)
        if v is None or not np.isfinite(v):
            continue
        val += w * v
        tot += w
    return float(val / tot) if tot > 0 else 0.0

def build_context(mkt: dict, asof: pd.Timestamp, live: bool = True,
                  ov_ret: dict | None = None, leader_pct: float | None = None) -> A.MarketContext:
    ctx = A.MarketContext(date=asof, bench=bench_row_at(mkt["bench"], asof, live))
    if ov_ret is not None:
        ctx.overseas = ov_ret
    else:
        # 实时分支才允许使用最新隔夜数据，回放分支必须留空，避免未来函数
        ctx.overseas = mkt["ov_ret"] if live else {}
    if leader_pct is not None:
        ctx.leader_pct = leader_pct
    else:
        ctx.leader_pct = mkt.get("leader_pct") if live else None

    ctx.panel_breadth = panel_breadth_at(mkt, asof, live)
    ctx.sector_stat = sector_stat_at(mkt["sec_frames"], asof, live)
    if live:
        ctx.internals = compute_breadth(mkt["spot"], mkt["limit"])
        ctx.capital = compute_capital(mkt["spot"])
        ctx.news_score = news_sentiment(mkt["news"])[0]
    return ctx

def prob_calib() -> dict:
    return {"base": 0.52, "lam": 0.75, "n": 0, **(meta_get("prob_calib", {}) or {})}

def apply_calib(p: float) -> float:
    """把模型概率向基准率收缩：p' = base + λ·(p - base)，λ 由历史 Brier 最优确定。"""
    c = prob_calib()
    q = c["base"] + float(c["lam"]) * (p - c["base"])
    return float(np.clip(q, *C.PROB_BAND))

def fit_prob_calib(min_n: int = 150) -> dict:
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT prob_raw,actual_pct FROM pred WHERE evaluated=1 AND actual_pct IS NOT NULL "
            "ORDER BY target DESC LIMIT 600").fetchall()]
    if len(rows) < min_n:
        return prob_calib()
    d = pd.DataFrame(rows)
    y = (d["actual_pct"] > 0).to_numpy(float)
    p = d["prob_raw"].fillna(0.52).to_numpy(float)
    base = float(np.clip(y.mean(), 0.35, 0.65))
    best_lam, best_bs = 1.0, 9e9
    for lam in np.arange(0.10, 1.35, 0.05):
        q = np.clip(base + lam * (p - base), C.PROB_BAND[0], C.PROB_BAND[1])
        bs = float(np.mean((q - y) ** 2))
        if bs < best_bs:
            best_lam, best_bs = float(lam), bs
    out = {"base": round(base, 4), "lam": round(best_lam, 3),
           "brier": round(best_bs, 4), "n": int(len(d)),
           "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    meta_set("prob_calib", out)
    return out

def gap_scenario(e_gap: float, min_n: int = 25) -> dict | None:
    """历史情景：与当前预判跳空幅度相似的日子里，日内与全天如何表现。"""
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT gap,intra,c2c FROM day_log").fetchall()]
    if len(rows) < 120:
        return None
    d = pd.DataFrame(rows)
    if e_gap <= -0.25:
        sub, lab = d[d["gap"] <= -0.25], "低开 0.25% 以上"
    elif e_gap >= 0.25:
        sub, lab = d[d["gap"] >= 0.25], "高开 0.25% 以上"
    else:
        sub, lab = d[(d["gap"] > -0.25) & (d["gap"] < 0.25)], "接近平开"
    if len(sub) < min_n:
        return None
    return {"label": lab, "n": int(len(sub)),
            "intra_mean": round(float(sub["intra"].mean()), 2),
            "intra_up": round(float((sub["intra"] > 0).mean()), 3),
            "c2c_mean": round(float(sub["c2c"].mean()), 2),
            "c2c_up": round(float((sub["c2c"] > 0).mean()), 3)}

def refit_models(window: int = 700, min_samples: int = 120) -> dict:
    """滚动重估三个部分：跳空回归、日内 logistic 权重、日内残差波动。"""
    out = {"ok": False, "n": 0}
    with db() as c:
        drows = [dict(r) for r in c.execute(
            "SELECT asof,ov,gap,intra,c2c FROM day_log ORDER BY asof DESC LIMIT ?",
            (window,)).fetchall()]
        srows = [dict(r) for r in c.execute(
            "SELECT asof,key,score,yi FROM sig_log WHERE live=0 ORDER BY asof DESC LIMIT ?",
            (window * len(A.HIST_EXPERTS),)).fetchall()]
    if len(drows) >= min_samples:
        d = pd.DataFrame(drows).sort_values("asof")
        X = d[["ov"]].to_numpy(float)
        yg = d["gap"].to_numpy(float)
        b, a, sd = A.fit_ols(X, yg)
        # 斜率向实证先验收缩，避免某段行情把系数拟合到极端
        b0 = float(np.clip(0.65 * b[0] + 0.35 * DEFAULT_GAP["b"], 0.02, 0.75))
        a0 = float(np.mean(yg - b0 * d["ov"].to_numpy(float)))
        meta_set("gap_model", {"a": round(a0, 4), "b": round(b0, 4),
                               "s": round(float(np.clip(sd, 0.2, 2.0)), 4), "n": int(len(d))})
        out["ok"] = True

    if len(srows) >= min_samples * 2:
        s = pd.DataFrame(srows)
        pv = s.pivot_table(index="asof", columns="key", values="score", aggfunc="last")
        yv = s.groupby("asof")["yi"].first()
        keys = A.HIST_EXPERTS
        pv = pv.reindex(columns=keys).fillna(0.0)
        yv = yv.reindex(pv.index).dropna()
        pv = pv.loc[yv.index]
        if len(pv) >= min_samples and yv.nunique() > 1:
            # 日内方向本身接近随机，强正则 + 权重截断，防止拟合噪声后抵消跳空的正确判断
            w, b0 = A.fit_logistic_multi(pv.to_numpy(float), yv.to_numpy(float), l2=6.0)
            learner = load_learner()
            for k, v in zip(keys, w):
                learner.w[k] = float(np.clip(v, -0.6, 0.6))
            learner.b = float(np.clip(b0, -0.55, 0.55))
            save_learner(learner, day=str(pv.index[-1]))

            # 日内幅度：以 z 为中心的线性标定，斜率同样强收缩
            zs = pv.to_numpy(float) @ np.array([learner.w[k] for k in keys]) + learner.b
            zs = zs - learner.b
            dd = pd.DataFrame({"asof": pv.index, "z": zs}).merge(
                pd.DataFrame(drows)[["asof", "intra"]], on="asof", how="inner")
            if len(dd) >= 60:
                k_, mu, sd_i = A.fit_ols(dd[["z"]].to_numpy(float), dd["intra"].to_numpy(float))
                meta_set("intra_model", {
                    "mu": round(float(np.clip(mu, -0.05, 0.15)), 4),
                    "k": round(float(np.clip(k_[0], -0.20, 0.20)), 4),
                    "s": round(float(np.clip(sd_i, 0.5, 2.5)), 4),
                    "n": int(len(dd))})
            out.update({"n": int(len(pv)), "b": round(learner.b, 4),
                        "w": {k: round(learner.w[k], 4) for k in keys}})
    out["calib"] = fit_prob_calib()
    out["interval"] = fit_interval()
    out["intra_edge"] = fit_intra_edge()
    _log_params()
    return out

def interval_calib() -> dict:
    return {"q": None, "cov": None, "n": 0, **(meta_get("interval", {}) or {})}

def fit_interval(q: float = 0.92, horizon: int = 250, min_n: int = 120) -> dict:
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT exp_ret,actual_pct FROM pred WHERE evaluated=1 AND actual_pct IS NOT NULL "
            "ORDER BY target DESC LIMIT 400").fetchall()]
    if len(rows) < min_n:
        return interval_calib()
    d = pd.DataFrame(rows)
    resid = (d["actual_pct"] - d["exp_ret"]).abs().to_numpy(float)
    half = float(np.quantile(resid, q))
    recent = d.head(horizon)
    cov = float(((recent["actual_pct"] - recent["exp_ret"]).abs() <= half).mean())
    out = {"q": round(half, 3), "cov": round(cov, 3), "n": int(len(d)),
           "target": 0.90, "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    meta_set("interval", out)
    return out

def fit_intra_edge(horizon: int = 120, min_n: int = 60) -> dict:
    # 日内层相对滚动基准率的 logloss 边际：为负说明有用，为正说明不如闭嘴
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT p_intra,actual_intra FROM pred WHERE evaluated=1 AND p_intra IS NOT NULL "
            "AND actual_intra IS NOT NULL ORDER BY target DESC LIMIT ?", (horizon,)).fetchall()]
    if len(rows) < min_n:
        return {"edge": None, "n": len(rows), "active": None}
    d = pd.DataFrame(rows)
    y = (d["actual_intra"] > 0).to_numpy(float)
    p = np.clip(d["p_intra"].to_numpy(float), 1e-4, 1 - 1e-4)
    base = float(np.clip(y.mean(), 1e-4, 1 - 1e-4))

    def ll(z):
        return float(-np.mean(y * np.log(z) + (1 - y) * np.log(1 - z)))

    out = {"edge": round(ll(p) - ll(np.full_like(p, base)), 4),
           "base_ll": round(ll(np.full_like(p, base)), 4),
           "model_ll": round(ll(p), 4), "n": int(len(d)),
           "hit": round(float(np.mean((p > 0.5) == (y > 0))), 4),
           "active": bool(ll(p) < ll(np.full_like(p, base)))}
    meta_set("intra_edge", out)
    return out

def param_log() -> list:
    return meta_get("param_log", []) or []

def _log_params() -> None:
    # 记录每次重估后的关键参数，形成可审计的进化轨迹
    snap = {"day": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "gap_b": gap_model().get("b"), "gap_s": gap_model().get("s"),
            "intra_k": intra_model().get("k"), "intra_mu": intra_model().get("mu"),
            "lam": (meta_get("prob_calib", {}) or {}).get("lam"),
            "q90": interval_calib().get("q"),
            "edge": (meta_get("intra_edge", {}) or {}).get("edge")}
    log = param_log()
    log.append(snap)
    meta_set("param_log", log[-40:])

