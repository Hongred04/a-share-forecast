"""
深度层：把"预判"拆到可交易的最小单元，并如实标注每一层的信息量与可执行性。

设计原则（与预测层相反）：
  预测层追求"更准"，深度层追求"更清楚"——清楚哪一段有信息、哪一段能交易、
  哪一段必须承认无效。所有统计量都用滚动窗口估计，不用全样本拟合未来。

三个概念区分（本模块的全部意义所在）：
  可预测  ≠ 可交易     信息在成交时点已实现，就无法变现
  统计显著 ≠ 经济显著   |t|>4 但幅度 < 成本，仍然是零
  有信息  ≠ 有意义      方向准确率高，但方差贡献为零，对决策无影响
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

# 双边交易成本假设（ETF 佣金+价差；股指期货约 1.5bp，此处取保守值）
COST_BP = 5.0
# 结构性统计的滚动窗口（交易日）
STRUCT_WIN = 250


# ======================================================================================
# 零、收益分段准备（全模块统一用「百分数」，与 r1 口径一致）
# ======================================================================================
def prepare(bench: pd.DataFrame) -> pd.DataFrame:
    """补齐 gap / intra / c2c 三列（单位：%），并去重、按日期索引。"""
    d = bench.copy()
    if "date" in d.columns:
        d["date"] = pd.to_datetime(d["date"]).dt.normalize()
        d = d.set_index("date")
    d = d[~d.index.duplicated(keep="last")].sort_index()
    need = ("open", "high", "low", "close")
    if any(c not in d.columns for c in need):
        raise ValueError("bench 缺少 OHLC 列")
    d["gap"] = (d["open"] / d["close"].shift(1) - 1) * 100
    d["intra"] = (d["close"] / d["open"] - 1) * 100
    d["c2c"] = d["close"].pct_change() * 100
    return d


# ======================================================================================
# 一、三段分解：收益到底从哪里来
# ======================================================================================
@dataclass
class Segment:
    key: str
    label: str
    window: str          # 该段的成交约束
    mean_bp: float       # 全样本日均（bp）
    recent_bp: float     # 近 250 日日均
    std_bp: float
    win: float           # 上涨概率
    sharpe: float        # 年化


def segments(bench: pd.DataFrame) -> dict:
    """昨收→今开（隔夜）、今开→今收（日内）、昨收→今收（全天）三段。"""
    d = prepare(bench).dropna(subset=["gap", "intra"])
    rows = []
    specs = (
        ("overnight", "隔夜", "昨收→今开", "gap"),
        ("intraday", "日内", "今开→今收", "intra"),
        ("full", "全天", "昨收→今收", "c2c"),
    )
    for key, label, window, col in specs:
        s = d[col].dropna()
        if len(s) < 100:
            continue
        bp = s * 100
        recent = bp.iloc[-STRUCT_WIN:].mean() if len(bp) > STRUCT_WIN else bp.mean()
        shp = float(bp.mean() / (bp.std() + 1e-12) * math.sqrt(244))
        rows.append(asdict(Segment(
            key=key, label=label, window=window,
            mean_bp=round(float(bp.mean()), 2), recent_bp=round(float(recent), 2),
            std_bp=round(float(bp.std()), 2), win=round(float((s > 0).mean()), 3),
            sharpe=round(shp, 2),
        )))


    # 滚动曲线（近 250 日累计），供界面画图
    roll = pd.DataFrame({
        "date": d.index,
        "overnight": d["gap"].rolling(60).sum(),
        "intraday": d["intra"].rolling(60).sum(),
    }).dropna()
    series = {
        "date": [str(x)[:10] for x in roll["date"].iloc[-260:]],
        "overnight": [round(float(x), 3) for x in roll["overnight"].iloc[-260:]],
        "intraday": [round(float(x), 3) for x in roll["intraday"].iloc[-260:]],
    }
    return {"segments": rows, "series": series}


# ======================================================================================
# 二、条件日内分布：开盘后唯一可执行的信息
# ======================================================================================
GAP_BUCKETS = (
    (-99.0, -1.0, "低开>1%"),
    (-1.0, -0.3, "低开0.3~1%"),
    (-0.3, 0.3, "平开±0.3%"),
    (0.3, 1.0, "高开0.3~1%"),
    (1.0, 99.0, "高开>1%"),
)


def conditional_intraday(bench: pd.DataFrame, pred_gap: float | None = None) -> dict:
    """给定开盘时已实现的跳空，日内收益的条件分布。

    关键：同时给出"原始均值"与"相对无条件均值的偏离(anomaly)"。
    原始均值为正只是因为 A 股日内本身有正漂移，不是跳空带来的边际。
    """
    d = prepare(bench).dropna(subset=["gap", "intra"])
    base = float(d["intra"].mean() * 100)
    rows = []
    for lo, hi, label in GAP_BUCKETS:
        g = d["gap"]
        m = (g >= lo) & (g < hi)
        n = int(m.sum())
        if n < 15:
            rows.append({"label": label, "n": n, "mean_bp": None, "median_bp": None,
                         "anomaly_bp": None, "se_bp": None, "win": None, "net_bp": None,
                         "significant": False, "reliable": False})
            continue
        s = d.loc[m, "intra"] * 100                 # 已是 bp
        mean = float(s.mean())
        med = float(s.median())
        se = float(s.std() / math.sqrt(n))
        rows.append({
            "label": label, "n": n,
            "mean_bp": round(mean, 2),
            "median_bp": round(med, 2),            # 中位数：单个极端日不会把它带偏
            "anomaly_bp": round(mean - base, 2),
            "se_bp": round(se, 1),
            "win": round(float((s > 0).mean()), 3),
            "net_bp": round(mean - COST_BP, 2),
            # |t| > 2 才认为该桶与无条件均值有显著差异
            "significant": bool(abs(mean - base) / (se + 1e-9) > 2.0),
            # n < 60 时均值不可依赖：单日即可改变 4bp 以上
            "reliable": bool(n >= 60),
        })

    cur = None
    if pred_gap is not None:
        for r in rows:
            lo, hi, _ = next(b for b in GAP_BUCKETS if b[2] == r["label"])
            if lo <= pred_gap < hi:
                cur = r
                break
    return {"base_bp": round(base, 2), "rows": rows, "current": cur,
}


# ======================================================================================
# 三、波动预测：真正可预测的那一层
# ======================================================================================
def _ewma_var(r: pd.Series, lam: float = 0.94) -> tuple[pd.Series, float]:
    """RiskMetrics EWMA 方差。

    返回 (series, next)：
      series[i] = 用 i-1 及之前信息对第 i 日的方差预测（用于 QLIKE 评估）
      next      = 用最后一日实现值更新后，对"下一日"的预测（用于当日展示）
    两者差一步，混淆会导致预测永远滞后一天。
    """
    x = (r.fillna(0.0) ** 2).to_numpy()
    out = np.empty(len(x))
    v = float(np.nanmean(x[:20])) if len(x) > 20 else float(np.nanmean(x))
    for i in range(len(x)):
        out[i] = v                 # 先写"对 i 的预测"，再用 i 的实现值更新状态
        v = lam * v + (1 - lam) * x[i]
    return pd.Series(out, index=r.index), float(v)


def _clip_var(v: float, floor: float) -> float:
    return float(max(v, floor))


def vol_state(bench: pd.DataFrame) -> dict:
    """EWMA / 历史均值 / 朴素 三种波动预测的 QLIKE 对比，以及当前状态。"""
    d = prepare(bench).dropna(subset=["c2c"])
    r = d["c2c"] / 100.0                     # 换回小数
    rv = (r ** 2).clip(lower=1e-12)
    unc_var = float(r.var())
    floor = unc_var * 0.05                   # 防止预测方差趋零导致 QLIKE 爆炸

    ewma_ser, fc_var = _ewma_var(r.shift(1).fillna(0.0))
    models = {
        "EWMA(0.94)": ewma_ser,
        "MA22": r.shift(1).rolling(22).var(),
        "朴素(昨日)": (r.shift(1) ** 2),
    }
    unc = r.expanding(60).var().shift(1)

    def qlike(pred: pd.Series, act: pd.Series) -> float:
        sub = pd.concat([pred.rename("p"), act.rename("a")], axis=1).dropna()
        if len(sub) < 60:
            return float("nan")
        pv = sub["p"].clip(lower=floor)
        q = sub["a"] / pv
        return float((q - np.log(q.clip(lower=1e-12)) - 1).mean())

    table = []
    for name, p in models.items():
        q = qlike(p.iloc[-800:], rv.iloc[-800:])
        table.append({"model": name, "qlike": round(q, 4) if q == q else None})
    q_unc = qlike(unc.iloc[-800:], rv.iloc[-800:])
    table.append({"model": "无条件方差", "qlike": round(q_unc, 4) if q_unc == q_unc else None})
    table.sort(key=lambda x: (x["qlike"] is None, x["qlike"] or 9e9))

    fc_vol = math.sqrt(max(fc_var * 244, 1e-12)) * 100        # 年化 %
    daily = math.sqrt(max(fc_var, 1e-12)) * 100               # 日 %

    # 分位：当前预测 vs 历史预测（同口径），而非 vs 已实现
    hist_fc = np.sqrt(ewma_ser.clip(lower=1e-12)) * 100
    pct = float((hist_fc < daily).mean()) if len(hist_fc) > 100 else 0.5
    label = "低波" if pct < 1 / 3 else ("中波" if pct < 2 / 3 else "高波")

    # 预测分位：标准化残差的经验分布（比正态更可信的尾部）
    z = (r / np.sqrt(ewma_ser.clip(lower=1e-12))).replace([np.inf, -np.inf], np.nan).dropna()
    tail = None
    if len(z) > 200:
        zq = {q: float(np.quantile(z, q)) for q in (0.01, 0.05, 0.95)}
        tail = {
            "var95_bp": round(zq[0.05] * daily * 100, 1),
            "var99_bp": round(zq[0.01] * daily * 100, 1),
            "cvar95_bp": round(float(z[z <= zq[0.05]].mean()) * daily * 100, 1),
            "upside95_bp": round(zq[0.95] * daily * 100, 1),
            "skew": round(float(z.skew()), 2),
            "kurt": round(float(z.kurt()), 2),
        }
    return {
        "forecast_vol_pct": round(daily, 2),
        "annual_vol_pct": round(fc_vol, 1),
        "percentile": round(pct, 3),
        "regime": label,
        "qlike": table,
        "tail": tail,
    }


# ======================================================================================
# 四、状态条件基准率
# ======================================================================================
def regime_prior(bench: pd.DataFrame, shrink: float = 40.0) -> dict:
    """P(涨 | 波动状态)，向无条件上涨率收缩，避免小样本极端值。"""
    d = prepare(bench).dropna(subset=["c2c"])
    y = (d["c2c"] > 0).astype(float)
    base = float(y.mean())
    vr = d["c2c"].rolling(20).std().rolling(STRUCT_WIN, min_periods=80).rank(pct=True)
    cur_rank = float(vr.iloc[-1]) if pd.notna(vr.iloc[-1]) else 0.5

    rows = []
    for lo, hi, label in ((0, 1 / 3, "低波"), (1 / 3, 2 / 3, "中波"), (2 / 3, 1.01, "高波")):
        m = (vr >= lo) & (vr < hi)
        n = int(m.sum())
        if n < 20:
            rows.append({"label": label, "n": n, "up": None, "prior": None, "edge": None})
            continue
        up = float(y[m].mean())
        prior = (float(y[m].sum()) + shrink * base) / (n + shrink)
        rows.append({"label": label, "n": n, "up": round(up, 4),
                     "prior": round(prior, 4), "edge": round(prior - base, 4)})
    cur = next((r["label"] for r in rows
                if r["prior"] is not None and
                ((cur_rank < 1 / 3 and r["label"] == "低波") or
                 (1 / 3 <= cur_rank < 2 / 3 and r["label"] == "中波") or
                 (cur_rank >= 2 / 3 and r["label"] == "高波"))), "中波")
    return {"base_rate": round(base, 4), "rows": rows, "current": cur,
            "current_rank": round(cur_rank, 3)}


# ======================================================================================
# 五、截面因子：板块之间的 alpha 与它的成本门槛
# ======================================================================================
FACTORS = {
    "位置动量(pos20)": lambda g: g["pos20"],
    "中期动量(r20)": lambda g: g["r20"],
    "短期反转(-r1)": lambda g: -g["r1"],
    "低波(-vol20)": lambda g: -g["vol20"],
    "缩量(-量比)": lambda g: -g["vol_ratio_amt"],
}


def cross_section(mkt: dict, last_n: int = 400) -> dict:
    """板块横截面因子的 IC、t 值、五分层价差，并扣费。"""
    frames = mkt.get("sec_frames") or {}
    if len(frames) < 20:
        return {"rows": [], "note": "板块数据不足"}
    recs = []
    for code, f in frames.items():
        if f is None or f.empty:
            continue
        cols = [c for c in ("date", "r1", "r20", "vol20", "vol_ratio_amt", "pos20") if c in f]
        sub = f[cols].copy()
        sub["code"] = code
        recs.append(sub)
    long = pd.concat(recs, ignore_index=True)
    long["date"] = pd.to_datetime(long["date"]).dt.normalize()
    long = long.sort_values(["code", "date"])
    long["fwd"] = long.groupby("code")["r1"].shift(-1)
    long = long.dropna(subset=["fwd", "r1", "pos20"])
    long = long[long["fwd"].abs() < 11.5]
    dates = sorted(long["date"].unique())[-last_n:]
    long = long[long["date"].isin(dates)]
    cnt = long.groupby("date")["code"].transform("count")
    long = long[cnt >= 30]
    if long.empty:
        return {"rows": [], "note": "样本不足"}

    rows = []
    for name, fn in FACTORS.items():
        ics, spreads = [], []
        for _, g in long.groupby("date"):
            x = fn(g).replace([np.inf, -np.inf], np.nan)
            ok = x.notna() & g["fwd"].notna()
            if ok.sum() < 30 or x[ok].nunique() < 5:
                continue
            ic = x[ok].rank().corr(g.loc[ok, "fwd"].rank())
            if pd.notna(ic):
                ics.append(ic)
            q = pd.qcut(x[ok].rank(method="first"), 5, labels=False)
            hi = g.loc[ok][q == 4]["fwd"].mean()
            lo = g.loc[ok][q == 0]["fwd"].mean()
            if pd.notna(hi) and pd.notna(lo):
                spreads.append((hi - lo) * 100)      # % → bp
        if len(ics) < 60:
            continue
        ics = np.array(ics)
        sp = np.array(spreads)
        t = float(ics.mean() / (ics.std() / math.sqrt(len(ics)) + 1e-12))
        rows.append({
            "factor": name,
            "ic": round(float(ics.mean()), 4),
            "t": round(t, 2),
            "hit": round(float((ics > 0).mean()), 3),
            "spread_bp": round(float(sp.mean()), 1),
            "net_bp": round(float(sp.mean()) - 2 * COST_BP, 1),
            "sharpe": round(float(sp.mean() / (sp.std() + 1e-12) * math.sqrt(244)), 2),
            # Bonferroni：5 个因子，双尾 5% → |t| > 2.58
            "survives": bool(abs(t) > 2.58),
            "n_days": len(ics),
        })
    rows.sort(key=lambda r: -abs(r["t"]))
    return {"rows": rows, "pit_warning": "板块成分为当前快照，回测含前视偏差"}


# ======================================================================================
# 六、选择偏差门槛
# ======================================================================================
def selection_bias(n_trials: int = 29, days: int = 1300) -> dict:
    """零假设下，测试 N 个变体后"最漂亮结果"的期望 Sharpe。"""
    gamma = 0.5772

    def z(p: float) -> float:
        # 标准正态分位（Acklam 近似）
        if p <= 0 or p >= 1:
            return float("nan")
        a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        dd = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
              3.754408661907416e+00]
        pl, ph = 0.02425, 1 - 0.02425
        if p < pl:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                   ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
        if p > ph:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                    ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)

    zmax = (1 - gamma) * z(1 - 1 / n_trials) + gamma * z(1 - 1 / (n_trials * math.e))
    yrs = days / 244
    return {
        "n_trials": n_trials,
        "years": round(yrs, 1),
        "threshold": round(zmax / math.sqrt(yrs), 2),
    }


# ======================================================================================
# 七、决策层：把分布变成动作（或明确说"不动"）
# ======================================================================================
def decide(pl: dict, depth: dict) -> dict:
    """输出三类动作：风险仓位、执行时点、交易机会。三者独立判断。"""
    vol = depth.get("vol") or {}
    ci = depth.get("intraday") or {}
    seg = {(s["key"]): s for s in (depth.get("segments") or {}).get("segments", [])}

    fc_vol = vol.get("forecast_vol_pct") or 1.0
    # 风险预算：以 1.2% 日波动为满仓基准，波动越高仓位上限越低
    cap = float(np.clip(0.9 / max(fc_vol, 0.3), 0.15, 1.0))

    cur = ci.get("current") or {}
    net = cur.get("net_bp")
    cur_seg = seg.get("intraday") or {}
    base_net = float(cur_seg.get("mean_bp", 0) or 0) - COST_BP

    # 交易机会：只有条件期望显著高于无条件、且扣费为正，才算机会
    actionable = bool(net is not None and net > COST_BP and cur.get("significant"))
    edge_bp = float(net) if net is not None else 0.0

    # 执行建议：预判跳空决定"开盘该不该动手"，而不是"该不该看多"
    e_gap = float(pl.get("e_gap", 0) or 0)
    if e_gap <= -0.8:
        advice = "预计低开：要减仓不用等开盘后，但低开本身不是买入理由"
    elif e_gap >= 0.8:
        advice = "预计高开：想买可以等回落，别在开盘抢"
    else:
        advice = "预计平开：开盘买还是等一等，差别不大"

    if actionable:
        headline = f"盘中有机会：扣费后约 {edge_bp / 100:+.2f}%"
        trade = {"direction": "long" if edge_bp > 0 else "short",
                 "edge_bp": round(edge_bp, 1), "bucket": cur.get("label")}
    else:
        headline = "今天没有值得动手的机会"
        trade = None

    return {
        "actionable": actionable,
        "headline": headline,
        "risk_cap": round(cap, 2),
        "risk_level": vol.get("regime") or "—",
        "vol_pct": round(fc_vol, 2),
        "execution_advice": advice,
        "trade": trade,
        "baseline_intraday_net_bp": round(base_net, 1),
        "why": ("开盘的缺口 9:30 就走完了，追不上" if not actionable
                else "这种开盘下盘中走势明显偏离常态，扣掉手续费还有剩"),
    }


# ======================================================================================
# 汇总
# ======================================================================================
def snapshot(mkt: dict, bench: pd.DataFrame, pred_gap: float | None = None,
             n_trials: int = 29) -> dict:
    seg = segments(bench)
    vol = vol_state(bench)
    reg = regime_prior(bench)
    ci = conditional_intraday(bench, pred_gap)
    xs = cross_section(mkt)
    sb = selection_bias(n_trials, len(bench))
    return {
        "segments": seg,
        "vol": vol,
        "regime": reg,
        "intraday": ci,
        "xsection": xs,
        "selection_bias": sb,
        "cost_bp": COST_BP,
    }
