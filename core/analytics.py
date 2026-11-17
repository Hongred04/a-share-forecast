"""因子、专家信号与在线学习模型。

设计要点（来自实盘数据检验的结论）
1. 日频上单日动量几乎无效，3 日反转与外盘隔夜才是有效信号 → 权重必须可正可负；
2. 只有"历史可复现"的信号才允许进入回放评估，实盘独有信号（资金/新闻/个股广度）
   用先验小权重冷启动，随实盘样本再学习，避免回放与实盘口径不一致；
3. 概率由 logistic 在线学习直接输出，基准上涨率由偏置项 b 自动吸收，
   不再用 0.5 做人为居中。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config as C

EPS = 1e-9


# ======================================================================================
# 基础指标
# ======================================================================================
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / (dn + EPS)
    return 100 - 100 / (1 + rs)


def macd_hist(close: pd.Series) -> pd.Series:
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    return (dif - dea) * 2


def safe_z(x: float, scale: float) -> float:
    if x is None or not np.isfinite(x):
        return 0.0
    return float(np.clip(x / (scale + EPS), -3, 3))


def tanh_score(raw: float, k: float = 1.0) -> float:
    return float(np.tanh(k * raw))


def sigmoid(x: float) -> float:
    return float(1 / (1 + np.exp(-np.clip(x, -30, 30))))


def robust_z(s: pd.Series) -> pd.Series:
    """横截面稳健标准化（中位数/MAD），抗极端值。

    这三种退化情形必须显式兜住，否则会往下游输出 NaN：
        · 单元素（成分股只剩 1 只通过过滤）—— pandas 对单元素求 std() 是 NaN
        · 全 NaN 列
        · 中位数与 MAD 都为 0（全同值）

    曾经写成 `scale = mad * 1.4826 if mad and mad > 0 else (x.std() or 1.0)`，
    看着像兜住了，实际没有：**NaN 在 Python 里是 truthy**，
    所以 `x.std() or 1.0` 在 std 为 NaN 时依然返回 NaN，整列 z 值全变 NaN，
    一路传到个股概率变成 `prob: NaN`，再被 json.dumps 写成非法 JSON 字面量，
    浏览器 JSON.parse 抛错，整页数据刷不出来。
    """
    x = pd.to_numeric(s, errors="coerce")
    med = x.median()
    mad = (x - med).abs().median()
    if mad is not None and np.isfinite(mad) and mad > 0:
        scale = mad * 1.4826
    else:
        scale = x.std()
        if scale is None or not np.isfinite(scale) or scale <= 0:
            scale = 1.0
    z = (x - med) / (scale + EPS)
    # 中位数本身为 NaN（整列无有效值）时上式仍会产出 NaN，这里统一压成 0：
    # "这一维没有可用信息"的正确含义是"不贡献分数"，而不是"让整个分数失效"。
    return z.fillna(0.0).clip(-3.5, 3.5)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """给单只标的的日线补齐技术指标列（向量化）。"""
    if df is None or len(df) < 30:
        return pd.DataFrame()
    d = df.copy()
    c, h, l, v, a = d["close"], d["high"], d["low"], d["volume"], d["amount"]
    d["r1"] = c.pct_change() * 100
    d["r3"] = c.pct_change(3) * 100
    d["r5"] = c.pct_change(5) * 100
    d["r10"] = c.pct_change(10) * 100
    d["r20"] = c.pct_change(20) * 100
    # 长周期：判断"跌了多久"必须有半年/一年尺度，只看 20 日会把深跌板块当成"弱势"
    d["r60"] = c.pct_change(60) * 100
    d["r120"] = c.pct_change(120) * 100
    for n in (5, 10, 20, 60):
        d[f"ma{n}"] = c.rolling(n).mean()
        d[f"dev{n}"] = (c / d[f"ma{n}"] - 1) * 100
    d["ma20_slope"] = d["ma20"].pct_change(5) * 100
    d["rsi6"] = rsi(c, 6)
    d["rsi14"] = rsi(c, 14)
    d["macd"] = macd_hist(c) / (c + EPS) * 100
    d["atr_pct"] = ((h - l) / (c + EPS) * 100).rolling(14).mean()
    d["atr_rank"] = d["atr_pct"].rolling(250, min_periods=60).rank(pct=True)
    d["vol20"] = d["r1"].rolling(20).std()
    d["amt20"] = a.rolling(20).mean().shift(1)
    d["vol_ratio_amt"] = a / (d["amt20"] + EPS)
    d["hi20"] = h.rolling(20).max()
    d["lo20"] = l.rolling(20).min()
    d["pos20"] = (c - d["lo20"]) / (d["hi20"] - d["lo20"] + EPS)
    # 一年尺度：回撤深度与区间位置。只看 20 日会把跌了半年的板块一律读成"弱势"，
    # 而这类板块的问题恰恰是位置，不是强弱。
    hi250 = h.rolling(250, min_periods=120).max()
    lo250 = l.rolling(250, min_periods=120).min()
    d["dd250"] = (c / hi250 - 1) * 100
    d["pos250"] = ((c - lo250) / (hi250 - lo250 + EPS)).clip(0, 1)
    # 筑底天数：最近一次创 60 日新低距今多少个交易日。0 = 今天还在创新低。
    # 用 60 日而非 250 日：一年尺度的"新低"对已经涨了两年的板块永远不触发，读不出筑底。
    lo60 = c.rolling(60, min_periods=60).min()
    at_low = (c <= lo60 * 1.0005).to_numpy()
    seen = np.where(at_low, np.arange(len(d)), -1)
    last_low = np.maximum.accumulate(seen)
    age = np.arange(len(d)) - last_low
    d["low_age"] = np.where(last_low < 0, np.nan, age).astype(float)
    up = (d["r1"] > 0).astype(int)
    grp = (up != up.shift()).cumsum()
    d["consec"] = up.groupby(grp).cumsum() * np.where(up == 1, 1, -1)
    d["amount"] = a
    d["volume"] = v
    return d


def at_index(df: pd.DataFrame, date: pd.Timestamp) -> int:
    """二分查找到 <= date 的最后一个位置，避免每日切片复制（回放性能关键）。"""
    if df is None or df.empty:
        return -1
    i = int(df["date"].searchsorted(pd.Timestamp(date), side="right")) - 1
    if i < 0:
        return -1
    if (pd.Timestamp(date) - pd.Timestamp(df["date"].iloc[i])).days > 6:
        return -1
    return i


def align_panel(frames: dict[str, pd.DataFrame], col: str = "pct") -> pd.DataFrame:
    series = {}
    for name, df in frames.items():
        if df is None or df.empty or col not in df:
            continue
        s = df.set_index("date")[col]
        series[name] = s[~s.index.duplicated(keep="last")]
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


# ======================================================================================
# 专家信号
# ======================================================================================
@dataclass
class Signal:
    key: str
    label: str
    score: float          # -1 ~ 1
    detail: str = ""
    available: bool = True
    live_only: bool = False


@dataclass
class MarketContext:
    """某一交易日的全部可得信息（实时与回放共用同一结构）。"""
    date: pd.Timestamp
    bench: dict = field(default_factory=dict)       # 上证当日指标行
    panel_breadth: dict | None = None               # 指数面板广度（历史可复现）
    internals: dict | None = None                   # 个股广度（实盘增量）
    capital: dict | None = None                     # 主力资金（实盘增量）
    sector_stat: dict | None = None                 # 板块扩散（历史可复现）
    news_score: float | None = None                 # 新闻情绪（实盘增量）
    overseas: dict = field(default_factory=dict)    # 隔夜外盘涨跌 %
    leader_pct: float | None = None                 # 美股龙头均值 %


def expert_trend(ctx: MarketContext) -> Signal:
    b = ctx.bench
    if not b:
        return Signal("trend", "趋势动量", 0, "数据缺失", False)
    z = (0.34 * safe_z(b.get("r5", 0), 3.2)
         + 0.26 * safe_z(b.get("dev20", 0), 2.6)
         + 0.22 * safe_z(b.get("macd", 0), 1.2)
         + 0.18 * safe_z(b.get("ma20_slope", 0), 1.4))
    detail = (f"5日{b.get('r5', 0):+.2f}%，20日乖离{b.get('dev20', 0):+.2f}%，"
              f"MA20斜率{b.get('ma20_slope', 0):+.2f}%")
    return Signal("trend", "趋势动量", tanh_score(z, 1.25), detail)


def expert_meanrev(ctx: MarketContext) -> Signal:
    b = ctx.bench
    if not b:
        return Signal("meanrev", "均值回归", 0, "数据缺失", False)
    overbought = safe_z(b.get("rsi6", 50) - 50, 18)
    pos = (b.get("pos20", 0.5) - 0.5) * 2
    r3 = safe_z(b.get("r3", 0), 2.4)
    raw = -(0.34 * overbought + 0.28 * pos + 0.38 * r3)
    detail = (f"3日{b.get('r3', 0):+.2f}%，RSI6={b.get('rsi6', 50):.0f}，"
              f"20日区间位置{b.get('pos20', 0.5):.0%}")
    return Signal("meanrev", "均值回归", tanh_score(raw, 1.0), detail)


def expert_overseas(ctx: MarketContext) -> Signal:
    if not ctx.overseas:
        return Signal("overseas", "隔夜外盘", 0, "数据缺失", False)
    wsum, tot, parts = 0.0, 0.0, []
    for name, w in C.OVERSEAS_CHAIN.items():
        v = ctx.overseas.get(name)
        if v is None or not np.isfinite(v):
            continue
        wsum += w * v
        tot += w
        parts.append(f"{name}{v:+.2f}%")
    if tot <= 0:
        return Signal("overseas", "隔夜外盘", 0, "数据缺失", False)
    eff = wsum / tot
    if ctx.leader_pct is not None and np.isfinite(ctx.leader_pct):
        eff = 0.82 * eff + 0.18 * ctx.leader_pct
        parts.append(f"美股龙头均值{ctx.leader_pct:+.2f}%")
    return Signal("overseas", "隔夜外盘", tanh_score(eff / 2.1, 1.0), "，".join(parts[:4]))


def expert_breadth(ctx: MarketContext) -> Signal:
    """指数面板广度：上涨指数占比、中位涨幅、小盘相对强弱（历史可复现）。"""
    pb = ctx.panel_breadth
    if not pb:
        return Signal("breadth", "指数广度", 0, "面板数据缺失", False)
    up_share = pb.get("up_share", 0.5)
    med = pb.get("median", 0.0)
    spread = pb.get("spread", 0.0)
    raw = 2.4 * (up_share - 0.5) * 2 + 0.7 * safe_z(med, 0.9) + 0.45 * safe_z(spread, 1.1)
    detail = (f"{up_share:.0%}的主要指数上涨，中位涨幅{med:+.2f}%，"
              f"小盘超额{spread:+.2f}%")
    return Signal("breadth", "指数广度", tanh_score(raw, 0.95), detail)


def expert_internals(ctx: MarketContext) -> Signal:
    """个股内部结构：涨跌家数、涨跌停、中位涨幅（仅实盘可得）。"""
    it = ctx.internals
    if not it:
        return Signal("internals", "个股结构", 0, "实盘数据缺失", False, True)
    up_ratio = it.get("up_ratio", 0.5)
    med = it.get("median_pct", 0.0)
    zt, dtc = it.get("limit_up", 0), it.get("limit_down", 0)
    raw = (2.9 * (up_ratio - 0.5) * 2
           + 0.8 * math.tanh((zt - dtc) / 40.0)
           + 0.55 * safe_z(med, 1.0))
    detail = (f"上涨家数占比{up_ratio:.0%}，中位涨幅{med:+.2f}%，"
              f"涨停{zt}家/跌停{dtc}家")
    return Signal("internals", "个股结构", tanh_score(raw, 0.8), detail, True, True)


def expert_capital(ctx: MarketContext) -> Signal:
    # unavailable：数据源（如新浪备用源）压根不提供主力净流入。
    # 不能让它走到下面的默认值——那会算出"净流入 +0 亿、占比 0%"，
    # 看上去像"今天资金没动"，实际是"这条数据不存在"。
    if not ctx.capital or ctx.capital.get("unavailable"):
        return Signal("capital", "主力资金", 0, "资金数据缺失（当前数据源不提供主力净流入）", False, True)
    ratio = ctx.capital.get("net_ratio", 0.0)
    pos_share = ctx.capital.get("pos_share", 0.5)
    net_yi = ctx.capital.get("net_yi", 0.0)
    raw = 3.0 * safe_z(ratio * 100, 3.2) + 1.9 * (pos_share - 0.5) * 2
    detail = f"主力净流入{net_yi:+.0f}亿，净流入个股占比{pos_share:.0%}"
    return Signal("capital", "主力资金", tanh_score(raw, 0.6), detail, True, True)


def expert_volume(ctx: MarketContext) -> Signal:
    b = ctx.bench
    if not b:
        return Signal("volume", "量能结构", 0, "数据缺失", False)
    r1 = b.get("r1", 0.0)
    vr = b.get("vol_ratio_amt", 1.0)
    # 备用源（腾讯日线）没有成交额，这一格会是 NaN。宁可判为不可用，
    # 也不要用 1.0 静默顶替——那等于把"没数据"说成"量能正常"。
    if vr is None or not np.isfinite(vr):
        return Signal("volume", "量能结构", 0, "成交额缺失", False)
    direction = 1.0 if r1 > 0 else -1.0
    strength = (vr - 1.0) * direction
    raw = float(np.clip(strength, -1.5, 1.5)) * 1.1
    if abs(r1) < 0.15:
        raw *= 0.3
    detail = f"成交额为20日均值{vr:.2f}倍，指数{r1:+.2f}%"
    return Signal("volume", "量能结构", tanh_score(raw, 0.8), detail)


def expert_sentiment(ctx: MarketContext) -> Signal:
    if ctx.news_score is None:
        return Signal("sentiment", "新闻情绪", 0, "新闻源不可用", False, True)
    return Signal("sentiment", "新闻情绪", tanh_score(ctx.news_score, 1.1),
                  f"快讯情绪得分{ctx.news_score:+.2f}", True, True)


def expert_rotation(ctx: MarketContext) -> Signal:
    """板块扩散：上涨板块占比、板块分化度、领涨板块动量（历史可复现）。"""
    st = ctx.sector_stat
    if not st:
        return Signal("rotation", "板块扩散", 0, "板块数据缺失", False)
    up_share = st.get("up_share", 0.5)
    dispersion = st.get("dispersion", 0.0)
    leader_mom = st.get("leader_mom", 0.0)
    raw = 2.2 * (up_share - 0.5) * 2 + 0.35 * safe_z(leader_mom, 3.0)
    if dispersion > 2.5:
        raw -= 0.3
    detail = (f"上涨板块占比{up_share:.0%}，离散度{dispersion:.2f}%，"
              f"领涨板块5日{leader_mom:+.2f}%")
    return Signal("rotation", "板块扩散", tanh_score(raw, 0.9), detail)


def expert_volregime(ctx: MarketContext) -> Signal:
    """波动率体制：ATR 在过去一年中的分位。实证上高波动状态次日收益偏高（波动聚集）。"""
    b = ctx.bench
    if not b or "atr_rank" not in b:
        return Signal("volregime", "波动率体制", 0, "数据缺失", False)
    rank = float(b["atr_rank"])
    score = float(np.clip(2 * rank - 1, -1, 1))
    detail = f"ATR处于近一年{rank:.0%}分位（{b.get('atr_pct', 0):.2f}%）"
    return Signal("volregime", "波动率体制", score, detail)


EXPERTS = [
    ("trend", "趋势动量", expert_trend),
    ("meanrev", "均值回归", expert_meanrev),
    ("overseas", "隔夜外盘", expert_overseas),
    ("breadth", "指数广度", expert_breadth),
    ("internals", "个股结构", expert_internals),
    ("capital", "主力资金", expert_capital),
    ("volume", "量能结构", expert_volume),
    ("volregime", "波动率体制", expert_volregime),
    ("sentiment", "新闻情绪", expert_sentiment),
    ("rotation", "板块扩散", expert_rotation),
]
EXPERT_LABELS = {k: lbl for k, lbl, _ in EXPERTS}
# 仅实盘可得信号的冷启动先验（回放无法评估，故给保守小权重）
LIVE_ONLY = {"internals", "capital", "sentiment"}
# 有历史样本、可被回放验证的信号（参与批量拟合）
HIST_EXPERTS = [k for k in EXPERT_LABELS if k not in LIVE_ONLY]
PRIOR_LIVE = 0.06


# ======================================================================================
# 在线学习：带符号权重的 logistic SGD
# ======================================================================================
def init_weights() -> dict[str, float]:
    return {k: (PRIOR_LIVE if k in LIVE_ONLY else 0.0) for k, _, _ in EXPERTS}


def combine(signals: list[Signal], w: dict[str, float],
            b: float = 0.0) -> tuple[float, list[Signal]]:
    avail = [s for s in signals if s.available]
    if not avail:
        return float(b), []
    for s in signals:
        s.score = float(np.clip(s.score, -1, 1))
    z = float(b) + sum(w.get(s.key, 0.0) * s.score for s in avail)
    return float(np.clip(z, -4, 4)), avail


class Learner:
    """w_i ← w_i - lr*(p-y)*s_i ；b ← b - lr*(p-y)

    带符号权重让模型能把"做反了"的信号翻转使用（如动量→反转），
    这是单靠加减权重无法实现的，也是"进化"的实质。
    """

    def __init__(self, w: dict[str, float] | None = None, b: float = 0.0,
                 lr: float = 0.012, l2: float = 5e-3, n: int = 0):
        self.w = dict(w if w is not None else init_weights())
        for k, _, _ in EXPERTS:
            self.w.setdefault(k, 0.0)
        self.b = float(b)
        self.lr = float(lr)
        self.l2 = float(l2)
        self.n = int(n)

    def logit(self, signals: list[Signal]) -> tuple[float, list[Signal]]:
        return combine(signals, self.w, self.b)

    def prob(self, signals: list[Signal]) -> tuple[float, float, list[Signal]]:
        z, avail = self.logit(signals)
        return sigmoid(z), z, avail

    def update(self, signals: list[Signal], y: int, lr: float | None = None) -> dict:
        eta = self.lr if lr is None else lr
        p, z, _ = self.prob(signals)
        g = p - float(y)
        contrib = {}
        for s in signals:
            if not s.available:
                continue
            old = self.w.get(s.key, 0.0)
            grad = g * s.score + self.l2 * old
            self.w[s.key] = float(np.clip(old - eta * grad, -1.8, 1.8))
            contrib[s.key] = round(self.w[s.key] - old, 4)
        self.b = float(np.clip(self.b - eta * g, -1.2, 1.2))
        self.n += 1
        return {"p": p, "y": y, "grad": round(float(g), 4), "z": round(float(z), 4),
                "dw": contrib}

    def to_dict(self) -> dict:
        return {"w": {k: round(float(v), 5) for k, v in self.w.items()},
                "b": round(float(self.b), 5), "n": self.n, "lr": self.lr}

    @classmethod
    def from_dict(cls, d: dict) -> "Learner":
        if not d:
            return cls()
        return cls(w=d.get("w"), b=d.get("b", 0.0), lr=d.get("lr", 0.035), n=d.get("n", 0))


def probability(z: float, band=C.PROB_BAND) -> float:
    return float(np.clip(sigmoid(z), band[0], band[1]))


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _erf_vec(x):
    """Abramowitz-Stegun 7.1.26 近似，误差 < 1.5e-7，用于向量化正态 CDF。"""
    x = np.asarray(x, dtype=float)
    sign = np.sign(x)
    ax = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * np.exp(-ax * ax)
    return sign * y


def norm_cdf_vec(x):
    return 0.5 * (1.0 + _erf_vec(np.asarray(x, dtype=float) / math.sqrt(2)))


def fit_ols(X: np.ndarray, y: np.ndarray, l2: float = 1e-6) -> tuple[np.ndarray, float, float]:
    """岭回归（含截距），返回 (系数, 截距, 残差标准差)。"""
    n, k = X.shape
    Xa = np.column_stack([X, np.ones(n)])
    reg = np.eye(k + 1) * l2
    reg[-1, -1] = 0.0
    try:
        beta = np.linalg.solve(Xa.T @ Xa + reg, Xa.T @ y)
    except np.linalg.LinAlgError:
        beta = np.zeros(k + 1)
    resid = y - Xa @ beta
    sd = float(np.std(resid, ddof=min(k + 1, max(n - 1, 1)))) if n > k + 2 else float(np.std(resid) or 1.0)
    return beta[:-1], float(beta[-1]), max(sd, 1e-3)


def fit_logistic_multi(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                       iters: int = 40) -> tuple[np.ndarray, float]:
    """带 L2 的多元 logistic 回归（牛顿-拉弗森）。返回 (系数, 截距)。

    相比逐日 SGD，批量拟合在噪声较大的日频数据上稳定得多；
    滚动重估保证只使用预测时点之前的信息。
    """
    n, k = X.shape
    Xa = np.column_stack([X, np.ones(n)])
    beta = np.zeros(k + 1)
    base = float(np.clip(y.mean(), 1e-4, 1 - 1e-4))
    beta[-1] = math.log(base / (1 - base))
    reg = np.eye(k + 1) * l2
    reg[-1, -1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Xa @ beta)))
        w = np.clip(p * (1 - p), 1e-6, None)
        grad = Xa.T @ (y - p) - reg @ beta
        hess = (Xa * w[:, None]).T @ Xa + reg
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta += np.clip(step, -2, 2)
        if np.max(np.abs(step)) < 1e-7:
            break
    return beta[:-1], float(beta[-1])


def direction_label(p: float, band: float = 0.02) -> str:
    """展示用三档；命中率按"上涨概率>50% 即看多"的二分口径统计。"""
    if p > 0.5 + band:
        return "偏多"
    if p < 0.5 - band:
        return "偏空"
    return "震荡"


def expected_move(z_alpha: float, vol: float, slope: float = 0.8) -> tuple[float, float, float]:
    e = float(np.clip(slope * z_alpha * vol, -2.6 * vol, 2.6 * vol))
    return e, e - 0.95 * vol, e + 0.95 * vol


# ======================================================================================
# 板块与个股打分
# ======================================================================================
def sector_cross_section(frames: dict[str, pd.DataFrame], date: pd.Timestamp,
                         overseas_pct: float, bench_r1: float) -> pd.DataFrame:
    """在给定日期生成板块横截面因子表（二分查找定位，回放同样可用）。"""
    rows = []
    for code, df in frames.items():
        i = at_index(df, date)
        if i < 25:
            continue
        last = df.iloc[i]

        def _f(col: str, default: float = float("nan")) -> float:
            v = last.get(col, default) if hasattr(last, "get") else default
            try:
                v = float(v)
            except (TypeError, ValueError):
                return default
            return v if np.isfinite(v) else default

        rows.append({
            "code": code,
            "name": last.get("name", code) if "name" in last.index else code,
            "r1": _f("r1", 0.0), "r3": _f("r3", 0.0),
            "r5": _f("r5", 0.0), "r20": _f("r20", 0.0),
            # 长周期用 NaN 表示"历史不够"，不要填 0：0 会被读成"半年没涨没跌"
            "r60": _f("r60"), "r120": _f("r120"),
            "dd250": _f("dd250"), "pos250": _f("pos250"), "low_age": _f("low_age"),
            "dev20": _f("dev20", 0.0), "pos20": _f("pos20", 0.5),
            "vol_ratio": _f("vol_ratio_amt", 1.0), "atr": _f("atr_pct", 2.0),
            "amount": _f("amount", 0.0),
            "excess1": _f("r1", 0.0) - bench_r1,
        })
    if not rows:
        return pd.DataFrame()
    cs = pd.DataFrame(rows)
    cs["us_beta"] = cs["name"].map(lambda n: C.SECTOR_US_BETA.get(str(n), C.DEFAULT_US_BETA))
    cs["us_shock"] = cs["us_beta"] * overseas_pct
    return cs


def score_sectors(cs: pd.DataFrame, bench5: float, market_exp: float = 0.0,
                  net: dict[str, float] | None = None,
                  live: dict[str, dict] | None = None,
                  etf: dict[str, dict] | None = None,
                  bench20: float = 0.0, bench120: float = 0.0) -> pd.DataFrame:
    """板块横截面打分 → 明日预期涨跌与上涨概率。

    概率不再与大盘脱钩：预期涨跌 = 大盘预期 + 该板块的相对强弱溢价，
    再除以板块自身波动得到概率，因此"大盘看空时最强板块也只是中性"是自然结果。
    """
    if cs.empty:
        return cs
    cs = cs.copy()
    cs["excess5"] = cs["r5"] - bench5
    z_rs = robust_z(cs["excess1"])
    z_m5 = robust_z(cs["excess5"])
    z_m20 = robust_z(cs["r20"])
    z_us = robust_z(cs["us_shock"])
    z_vr = robust_z(cs["vol_ratio"])
    z_pos = robust_z(cs["pos20"])
    hot_pen = robust_z((cs["pos20"] - 0.82).clip(lower=0) * 10)
    z_net = robust_z(cs["code"].map(lambda c: (net or {}).get(c, 0.0))) if net else 0.0

    s = (0.24 * z_rs + 0.22 * z_m5 + 0.13 * z_m20 + 0.13 * z_us
         + 0.09 * z_vr + 0.11 * z_net - 0.09 * z_pos - 0.07 * hot_pen)
    if live:
        nr = cs["code"].map(lambda c: (live.get(c) or {}).get("net_ratio"))
        ur = cs["code"].map(lambda c: (live.get(c) or {}).get("up_ratio"))
        s = s + 0.11 * robust_z(nr.fillna(0) * 100) + 0.09 * robust_z(ur.fillna(0.5) * 100)
    if etf:
        # ETF 资金动向：主力净占比 + 份额变化（若有）→ 机构通过 ETF 进出的痕迹
        e_flow = cs["name"].map(lambda n: (etf.get(n) or {}).get("net_ratio", 0.0))
        e_share = cs["name"].map(lambda n: (etf.get(n) or {}).get("share_chg", 0.0) or 0.0)
        s = s + 0.10 * robust_z(e_flow.fillna(0) * 100) + 0.06 * robust_z(e_share.fillna(0) * 100)

    cs["z"] = s
    cs["score"] = (50 + 50 * np.tanh(s / 1.6)).round(1)

    atr = pd.to_numeric(cs["atr"], errors="coerce").fillna(2.0).clip(0.6, 6.5)
    cs["tilt"] = (0.42 * np.tanh(s / 1.4) * (atr / 1.6).clip(0.6, 2.0)).clip(-0.9, 0.9)
    cs["exp_ret"] = float(market_exp) + cs["tilt"]
    sigma = (atr / 1.6).clip(0.7, 3.0)
    cs["prob"] = np.clip(norm_cdf_vec(cs["exp_ret"] / sigma), 0.03, 0.97)

    # ---- 超跌潜伏：跌得久 + 位置低 ----
    # 「明日概率」问的是明天涨不涨，「潜伏」问的是跌够了没有，两者不是同一把尺子，
    # 因此潜伏单独给分、不输出概率。判据全部相对大盘：全市场一起跌时，
    # 绝对跌幅无法区分"被资金抛弃"与"随大流"。
    # 入选只看"跌久 + 低位"；近 5 日是否转强、资金是否回流只决定排名，不决定入选，
    # 否则单边下跌日会一个都选不出来。
    cs["rel5"] = cs["r5"] - bench5
    cs["rel20"] = cs["r20"] - bench20
    cs["rel120"] = cs["r120"] - bench120
    # 入选按潜伏分取前 8，回撤深度做地板。
    # 曾经用"三个维度各取名次再平均"，问题是全市场一起跌时 pos250 全挤在 0.06~0.17，
    # 名次把这个区间放大成巨大差异，反而把"跌了半年、刚启动"的板块（如电力设备）挤出去。
    # 连续打分不会因为某一维挤在一起而失真。
    pot_s = (0.34 * robust_z(-cs["rel120"]) + 0.24 * robust_z(-cs["dd250"])
             + 0.20 * robust_z(1 - cs["pos250"]) + 0.22 * robust_z(cs["rel5"]))
    cs["pot_score"] = (50 + 50 * np.tanh(pot_s / 1.5)).round(1)
    cs["pot"] = ((cs["pot_score"].rank(ascending=False, method="min") <= C.SECTOR_POT_POOL)
                 & (cs["dd250"] <= -15)).fillna(False).astype(bool)

    # 排序按上涨概率：概率已内含波动率差异，避免"预期幅度大但概率低"的板块排在前面
    cs = cs.sort_values("prob", ascending=False).reset_index(drop=True)
    cs["rank"] = np.arange(1, len(cs) + 1)
    return cs


def name_theme_scan(df: pd.DataFrame, up_pct: float = 5.0, min_n: int = 4,
                    min_lift: float = 3.0, top: int = 4) -> list[dict]:
    """扫"名称炒作"：今天涨停的票，名字里有没有共同的字。

    为什么要有这个
        市场隔一段时间就会按**名字**炒一轮——"东方"系、"洋"字辈、"龙"字头。
        这类行情跟行业、业绩、资金都没有关系，十只股票可能分散在十个
        互不相关的行业里（实测：厨卫电器、农产品加工、饲料、塑料、
        通用设备、医疗服务、燃气、电力、一般零售、照明设备）。
        系统的板块是行业/概念分类，**结构上不可能**把它们归到一起，
        所以"板块预判里看不到东方系"不是缺陷，是分类维度不同。
        但读者需要知道今天市场在按什么方式定价，否则会以为系统漏了行情。

    这个结果不参与任何预测
        名称炒作是纯情绪博弈：没有基本面锚，退潮时跌幅与涨幅对称，
        事前也无法知道哪天开始、哪天结束。把它做成选股因子，
        等于用"我已经看到它涨了"去解释"它为什么涨"，是数据窥探。
        所以这里只做**描述**（今天在炒什么字），不给方向、不进概率。

    怎么找
        用 lift 而不是词频：某字在涨停股里的出现率 ÷ 它在全市场的出现率。
        纯词频会把"科技""股份"排在最前——它们到处都是，没有信息量。
        lift 高才说明"这个字今天异常集中"。这样也不用维护一张
        "哪些字是通用词"的黑名单，黑名单永远补不全。

        同时统计单字与相邻两字：
            "洋"  → 澳洋健康 / 百洋股份 / 天洋新材 / 大西洋 / 海洋王
            "东方" → 东方海洋 / 东方环宇 / 大东方 / 日出东方
        只统计单字会漏掉"东方"（"东"在全市场太常见，lift 上不去）；
        只统计两字会漏掉"洋"（"洋"多为中间字，两字组合各不相同）。
    """
    if df is None or df.empty or "name" not in df.columns:
        return []
    hot = df[pd.to_numeric(df.get("pct"), errors="coerce") >= up_pct]
    if len(hot) < min_n:
        return []

    def counts(names) -> dict[str, int]:
        c: dict[str, int] = {}
        for nm in names:
            s = str(nm)
            grams = set()
            for i, ch in enumerate(s):
                grams.add(ch)
                if i + 1 < len(s):
                    grams.add(s[i:i + 2])
            for g in grams:
                c[g] = c.get(g, 0) + 1
        return c

    ch_hot, ch_all = counts(hot["name"]), counts(df["name"])
    n_hot, n_all = len(hot), len(df)
    has_ind = "industry" in hot.columns
    out = []
    for g, k in ch_hot.items():
        if k < min_n or k >= n_hot:       # 全部涨停股都带这个字 → 是巧合而非主题
            continue
        if not all("\u4e00" <= ch <= "\u9fff" for ch in g):
            continue
        p_all = ch_all.get(g, 0) / n_all
        if p_all <= 0:
            continue
        lift = (k / n_hot) / p_all
        if lift < min_lift:
            continue
        names = [str(nm) for nm in hot["name"] if g in str(nm)]
        # 跨行业才叫"名称炒作"。如果命中的票大多同属一个行业（例如涨停里
        # 5 只都带"新材"），那是**板块效应**——新材料板块在涨，不是市场在炒名字。
        # 不区分的话"新材""智能"这类行业词会把真正的名称主题挤下去。
        if has_ind:
            inds = hot.loc[hot["name"].astype(str).str.contains(g, regex=False),
                           "industry"].dropna().astype(str)
            if len(inds) >= 3:
                share = float(inds.value_counts().iloc[0]) / len(inds)
                if share > 0.6:
                    continue
        out.append({"label": g, "n": k, "lift": round(lift, 1),
                    "names": names[:6]})
    # 按集中度排序，不按词长：lift 高的才是今天真正异常的那个字。
    # 先按长度排会让"新材"（2 字）压过"洋"（1 字、lift 高得多）。
    out.sort(key=lambda x: -x["lift"])
    picked = []
    for it in out:
        if any(it["label"] in u["label"] or u["label"] in it["label"] for u in picked):
            continue
        picked.append(it)
        if len(picked) >= top:
            break
    return picked


def score_stocks(df: pd.DataFrame, sector_pct: float, sector_prob: float,
                 calib: dict | None = None,
                 amount_floor: float = C.STOCK_MIN_AMOUNT) -> pd.DataFrame:
    """板块内个股打分：相对强度 + 资金 + 量价结构 + 风险过滤。

    amount_floor 是**全日当量**的流动性下限，调用方负责把盘中累计成交额
    折算后再传进来（见 predict.py 里的 _elapsed_fraction）。
    不折算的后果实测过：09:29 集合竞价时全市场成交额才几千万，
    1.2 亿的门槛把 55 只候选滤到只剩 1 只，9 只个股最后只出了 4 只。

    概率是两段式：
        1. raw —— 板块基准概率做 anchor，个股分数推一个 logit 偏移。
           这是"先验"，不含任何历史成绩。
        2. prob —— 用 raw 经校准层得到，校准参数来自**已评价的真实样本**。
           样本不足时（n < MIN_N）退回 raw，并在 payload 里标 `calibrated=False`。

    为什么必须分两段
        个股没有历史横截面数据（回测里成分股是空的），所以无法像大盘那样做
        800 日回放。能依赖的只有"每天选出来的这几只，第二天实际涨没涨"。
        也就是说校准只能**前向积累**，第一天必然没有样本。既然如此，
        就必须让"没校准"这个状态对外可见，而不是假装那个 0.65 是可信的。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    # 排除北交所（4/8/920 开头）与新三板。四个理由，每个都独立成立：
    #   1) 涨跌幅 30%，风险特征和主板/创业板完全不同，混在一起算概率没有意义；
    #   2) 多数账户没开通权限，推荐了也买不了；
    #   3) 成交清淡，1.2 亿当量门槛刚过线的标的滑点极大；
    #   4) 东财 K 线接口对 920xxx 返回 {"data": null}，拿不到历史就没法评价，
    #      校准闭环里永远是缺失项——宁可一开始就不选。
    d = d[~d["code"].astype(str).str.startswith(("4", "8", "920"))]
    d = d[(d["price"] > 2) & (d["amount"] > amount_floor)]
    d = d[~d["name"].astype(str).str.contains("ST|退|N |C ", na=False)]
    d = d[(d["pe"].isna()) | (d["pe"] > 0)]
    if "list_date" in d:
        ld = pd.to_numeric(d["list_date"], errors="coerce")
        cutoff = float(pd.Timestamp.today().strftime("%Y%m%d")) - 8900
        d = d[(ld.isna()) | (ld < cutoff)]
    if d.empty:
        return d
    d["rel"] = d["pct"] - sector_pct
    d = d[~(d["pct"] >= 19.3)]
    if d.empty:
        return d
    z_rel = robust_z(d["rel"])
    z_net = robust_z(d["net_ratio"].fillna(0) * 100)
    vr = d["vol_ratio"].fillna(1.0)
    z_vr = robust_z(-(np.log(vr.clip(0.3, 6)) - np.log(1.8)).abs())
    to = d["turnover"].fillna(3.0)
    z_to = robust_z(-(np.log(to.clip(0.3, 40)) - np.log(6.5)).abs())
    mc = (d["float_mcap"].fillna(d["mcap"]).fillna(1e10) / 1e8)
    z_mc = robust_z(-(np.log(mc.clip(5, 20000)) - np.log(180)).abs())
    z_pe = robust_z(-(np.log(d["pe"].fillna(35).clip(3, 300)) - np.log(30)).abs())

    s = 0.30 * z_rel + 0.26 * z_net + 0.14 * z_vr + 0.10 * z_to + 0.10 * z_mc + 0.10 * z_pe
    d["z"] = s
    p_sec = float(np.clip(sector_prob, 0.05, 0.95))
    anchor = math.log(p_sec / (1 - p_sec))
    raw = 1 / (1 + np.exp(-(anchor + 0.32 * np.tanh(0.50 * s))))
    raw = raw.clip(max(p_sec - 0.08, 0.18), min(p_sec + 0.08, 0.78))
    d["prob_raw"] = raw

    d["prob"], d["calibrated"] = apply_stock_calib(raw, calib)
    d["score"] = (50 + 50 * np.tanh(s / 1.8)).round(1)
    return d.sort_values("z", ascending=False).reset_index(drop=True)


# 校准起步门槛。低于这个样本数不算命中率、不调参——十几个样本拟合出来的
# 参数只会把噪音固化成"模型"，比不校准更危险。
STOCK_CALIB_MIN_N = 40

# 校准前的保守先验：把 raw 往 0.5 猛拉（lam 小）。
# 理由是个股概率天然过窄（实测 9 只挤在 0.644~0.698），
# 而真实命中率并没有那么高——不收缩就会系统性过度自信。
DEFAULT_STOCK_CALIB = {"base": 0.50, "lam": 1.00, "slope": 1.0, "n": 0}


def apply_stock_calib(raw, calib: dict | None = None):
    """把 raw 概率校准到真实命中率上。

    p' = base + lam * (raw - base)，再做一次 logit 斜率修正。
    lam < 1 表示"不相信 raw 的极端程度"——这是校准的主要作用。

    未达样本量时 lam=1.0（恒等），即原样输出 raw——这不是"相信 raw"，
    而是"没有证据就不动它"。假装已经校准（拿一个凭空的 lam=0.35 去压缩）
    会让界面上的 0.65 变成一个既非公式原值、也非实测校准值的第三方数字，
    那就无法回答"这个概率到底是算出来的还是调出来的"。
    宁可让它难看，也要让它可追溯。
    """
    c = {**DEFAULT_STOCK_CALIB, **(calib or {})}
    ready = int(c.get("n", 0)) >= STOCK_CALIB_MIN_N
    # 未就绪就强制恒等：不能出现"标注说未校准、数字却已经被收缩"的情况。
    # 这两种状态在界面上是同一个数字，一旦打架就无法核对——
    # 标注说 0.68 是公式原值，实际却是某个 lam 调过的值，读者无从分辨。
    if not ready:
        p = np.clip(np.asarray(raw, dtype=float), 0.02, 0.98)
        return p, False
    base = float(c.get("base", 0.50))
    lam = float(c.get("lam", 1.00))
    slope = float(c.get("slope", 1.0))
    p = np.clip(np.asarray(raw, dtype=float), 0.02, 0.98)
    q = base + lam * (p - base)
    if abs(slope - 1.0) > 1e-9:
        q = np.clip(q, 1e-4, 1 - 1e-4)
        lg = np.log(q / (1 - q)) * slope
        q = 1 / (1 + np.exp(-lg))
    q = np.clip(q, 0.12, 0.82)
    return q, True


def fit_stock_calib(rows: list[dict], min_n: int = STOCK_CALIB_MIN_N) -> dict | None:
    """用"选出过的个股 + 真实涨跌"拟合校准参数。

    样本来自 stock_pred 表：每天选出 9 只左右，被评价后拿到 actual_pct。
    这是**前向**样本，不是回放样本——所以 n 增长得慢（每个交易日 +9），
    到 40 个样本大约需要 5 个交易日。这个速度是诚实的，也说明前期必须
    把"未校准"标出来。

    拟合方式：base 固定为实测命中率，网格只搜 (lam, slope)，目标 Brier 最小。

    base 为什么不一起搜
        搜过，结果是 base 被拖到搜索区间下限之外（0.347，而真实命中率 0.407）。
        原因是 base 与 lam 在数学上不可分辨：lam 越小、q 越贴近 base，
        于是"把 base 压低"和"把 lam 压小"对 Brier 的效果几乎一样，
        两者会互相补偿，base 就不再是"基准率"，而变成第二个收缩旋钮。
        固定 base = 命中率之后，base 可解释（就是这批股票第二天涨的比例），
        lam 单独负责"要不要相信 raw 的偏离"，各管一件事。
    """
    pts = [(float(r["prob_raw"]), 1.0 if float(r["actual_pct"]) > 0 else 0.0)
           for r in rows
           if r.get("prob_raw") is not None and r.get("actual_pct") is not None]
    if len(pts) < min_n:
        return None
    p = np.array([x[0] for x in pts])
    y = np.array([x[1] for x in pts])
    base = float(np.clip(y.mean(), 0.30, 0.70))
    hit_rate = float(y.mean())

    best = None
    # lam 下限 0.20 而不是 0：lam→0 时所有个股被压成同一个数字，
    # 界面会出现"9 只股票概率一模一样"，读者只会认为程序坏了，
    # 或者随手挑一只——两种结果都比给个略微分散的数更糟。
    # 保留 0.20 的最小区分度，同时用下面的 spread/weak 把"区分度本来就低"
    # 这件事明说出来，而不是靠数字上的一点点差异糊过去。
    for lam in np.arange(0.20, 1.05, 0.05):
        for slope in np.arange(0.6, 1.45, 0.1):
            q, _ = apply_stock_calib(p, {"base": base, "lam": lam, "slope": slope,
                                         "n": min_n})
            bs = float(np.mean((np.asarray(q) - y) ** 2))
            if best is None or bs < best[0]:
                best = (bs, float(lam), float(slope))
    if best is None:
        return None
    bs, lam, slope = best
    # 对照：不校准时的 Brier。拟合后没有明显改善就不改参数——
    # 免得拿一堆噪声换一个"看起来更专业"的数字。
    raw_bs = float(np.mean((p - y) ** 2))
    q_best, _ = apply_stock_calib(p, {"base": base, "lam": lam, "slope": slope, "n": min_n})
    spread = float(np.max(q_best) - np.min(q_best))
    return {"base": round(base, 4), "lam": round(lam, 3), "slope": round(slope, 3),
            "brier": round(bs, 4), "brier_raw": round(raw_bs, 4),
            "hit_rate": round(hit_rate, 4), "n": int(len(pts)),
            "spread": round(spread, 4),
            # weak：校准后概率几乎分不开。这不是故障，是"个股打分目前
            # 跟次日涨跌没有稳定关系"的实测结论，必须让界面能说出来。
            "weak": bool(spread < 0.02 or lam <= 0.21),
            "improved": bool(bs < raw_bs - 1e-4)}


def stock_reasons(row: pd.Series, sector_name: str, sector_pct: float) -> list[str]:
    """给个股一句话理由。全部用大白话："跑赢板块"改成"比板块多涨"，"量比"改成倍数。"""
    out = []
    rel = row["pct"] - sector_pct
    if rel > 1.0:
        out.append(f"比板块多涨 {rel:.1f}%")
    elif rel > -0.3:
        out.append("跟板块同步，没掉队")
    nr = row.get("net_ratio")
    if nr is not None and np.isfinite(nr) and nr > 1.5:
        out.append(f"大资金净买入占 {nr:.1f}%")
    vr = row.get("vol_ratio")
    if vr is not None and np.isfinite(vr) and 1.1 <= vr <= 3.0:
        out.append(f"成交是平时 {vr:.1f} 倍")
    to = row.get("turnover")
    if to is not None and np.isfinite(to) and 2 <= to <= 12:
        out.append(f"换手 {to:.1f}%")
    mc = row.get("float_mcap")
    if mc is not None and np.isfinite(mc):
        out.append(f"市值 {mc / 1e8:.0f} 亿")
    pe = row.get("pe")
    if pe is not None and np.isfinite(pe) and 0 < pe < 45:
        out.append(f"市盈率 {pe:.0f} 倍")
    return out[:4]
