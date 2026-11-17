"""缠论结构计算（精简版）：包含处理 → 分型 → 笔 → 中枢 → 背驰 → 买卖点。

为什么要有这个文件
    用户要求把缠论纳入预测。但缠论的大部分争议都在"事后回溯"里：
    同一段 K 线，不同人画出的笔和中枢可以不一样。要做量化，先得把
    规则钉死到"给定输入必然得到同一输出"，否则回测无从复现。

只实现能客观判定的部分
    包含处理、分型、笔、中枢、MACD 背驰、三类买卖点——这些有明确的几何定义。
    **线段**没有实现：它依赖"特征序列 + 缺口"的多种流派处理，同一段走势
    不同实现能差出两三根，属于主观区间。既然要做的是"能不能预测"，
    就用争议最小的单元（笔）做中枢，把自由度和解释空间一起压掉。

最重要的一点：每个结构都带 confirmed_at
    缠论的东西全是**事后确认**的：
      · 分型要等右边那根 K 线走完（否则不知道中间那根是不是极值）
      · 笔要等下一个分型出现（否则不知道这笔会不会继续延伸）
      · 中枢要等第三段走完
      · 三买要等回抽段走完
    如果回测时用"结构自己的结束位置"当信号时点，等于用今天才知道的笔
    去交易上周——这是缠论量化最常骗自己的地方，也是最容易做出漂亮曲线的地方。
    所以每个结构都记 `confirmed_at` = 第几根原始 K 线**收盘后**才算数，
    回测一律从 confirmed_at 的下一根开始算收益。

口径
    输入是带 date/open/high/low/close 的日线 DataFrame，索引 0..n-1。
    confirmed_at 就是这个索引，指向"收盘后已经可以据此下单"的那一天。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# 笔的最小跨度：两个分型"中心"合并 K 线的下标差。
# 4 = 顶底之间至少有 1 根独立 K 线（含两端共 5 根），即"老笔"。
# 取"新笔"的 3 会让笔的数量翻倍，但绝大多数是噪音，先用严格口径。
MIN_BI_GAP = 4

# 背驰阈值：后段 MACD 面积必须小于前段的 90% 才算力度衰减。
# 1.0 会把"面积持平"也算背驰，信号量翻倍而质量下降；0.8 又太严。
BEICHI_RATIO = 0.9


@dataclass
class Bar:
    """包含处理后的合并 K 线。"""
    idx: int                     # 在合并序列中的位置
    high: float
    low: float
    elements: list[int]          # 由哪些原始 K 线合并而来
    confirmed_at: int            # 最后一根原始 K 线走完的时点
    hi_idx: int = 0              # 最高价来自哪根原始 K 线
    lo_idx: int = 0              # 最低价来自哪根原始 K 线


@dataclass
class Fx:
    """分型。"""
    kind: str                    # 'top' / 'bottom'
    m_idx: int                   # 合并序列中的中心位置
    high: float
    low: float
    value: float                 # 顶用 high，底用 low
    raw_idx: int                 # 极值所在的原始 K 线
    confirmed_at: int


@dataclass
class Bi:
    """笔。"""
    idx: int
    direction: str               # 'up' / 'down'
    a: Fx
    b: Fx
    high: float
    low: float
    raw_start: int
    raw_end: int
    confirmed_at: int


@dataclass
class ZS:
    """中枢。"""
    idx: int
    ZG: float                    # 上沿 = min(各段高点)
    ZD: float                    # 下沿 = max(各段低点)
    GG: float
    DD: float
    bi_from: int
    bi_to: int
    confirmed_at: int            # 第三段确认时成立
    closed_at: int | None = None # 被某根笔离开、中枢结束时


@dataclass
class Signal:
    """买卖点。"""
    kind: str                    # buy1 / buy2 / buy3 / sell1 / sell2 / sell3 / pzbuy / pzsell
    bi_idx: int
    price: float
    confirmed_at: int
    reason: str
    ratio: float | None = None   # 背驰力度比（后段/前段）


@dataclass
class ChanResult:
    bars: list[Bar] = field(default_factory=list)
    fractals: list[Fx] = field(default_factory=list)
    bis: list[Bi] = field(default_factory=list)
    zhongshus: list[ZS] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. 包含处理
# ---------------------------------------------------------------------------
def _included(h1: float, l1: float, h2: float, l2: float) -> bool:
    return (h1 >= h2 and l1 <= l2) or (h2 >= h1 and l2 <= l1)


def merge_bars(df: pd.DataFrame) -> list[Bar]:
    """K 线包含处理。

    方向由"被包含前那根与它的前一根"决定，而不是拍一个全局方向：
    上升途中取高高、下降途中取低低，这是缠论原文的口径（课 62/65）。
    合并后每根 bar 的 confirmed_at 取它最后一根原始 K 线的下标——
    在那之前，这根合并 K 线的高低压根没定，用它做判断就是偷看。
    """
    out: list[Bar] = []
    if df is None or df.empty:
        return out
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    last_dir = "up"
    for i in range(len(df)):
        if not out:
            out.append(Bar(0, hi[i], lo[i], [i], i, hi_idx=i, lo_idx=i))
            continue
        last = out[-1]
        if not _included(last.high, last.low, hi[i], lo[i]):
            last_dir = "up" if hi[i] > last.high else "down"
            out.append(Bar(len(out), hi[i], lo[i], [i], i, hi_idx=i, lo_idx=i))
            continue
        # 有包含：按当前方向并入 last。
        # 极值来源要跟着一起更新——否则后面算 MACD 面积时，
        # 区间的端点会指向错误的原始 K 线，背驰力度整个算偏。
        direction = ("up" if last.high > out[-2].high else "down") if len(out) >= 2 else last_dir
        if direction == "up":
            if hi[i] > last.high:
                last.high, last.hi_idx = hi[i], i
            if lo[i] > last.low:
                last.low, last.lo_idx = lo[i], i
        else:
            if hi[i] < last.high:
                last.high, last.hi_idx = hi[i], i
            if lo[i] < last.low:
                last.low, last.lo_idx = lo[i], i
        last.elements.append(i)
        last.confirmed_at = i
        last_dir = direction
    return out


# ---------------------------------------------------------------------------
# 2. 分型
# ---------------------------------------------------------------------------
def find_fractals(bars: list[Bar]) -> list[Fx]:
    """三根相邻合并 K 线定分型，右侧那根走完才算数。

    顶分型要求 b 的高点最高**且低点也最高**（严格分型）。只比 high 会把
    "高点略高但下影很长的震荡"也算成顶，那种分型后面接的不是转折而是继续。
    """
    out: list[Fx] = []
    for i in range(1, len(bars) - 1):
        a, b, c = bars[i - 1], bars[i], bars[i + 1]
        if b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low:
            kind, val = "top", b.high
        elif b.high < a.high and b.high < c.high and b.low < a.low and b.low < c.low:
            kind, val = "bottom", b.low
        else:
            continue
        out.append(Fx(kind=kind, m_idx=i, high=b.high, low=b.low, value=val,
                      # 极值来自中心那根 bar 的具体原始 K 线（不是确认点）
                      raw_idx=(b.hi_idx if kind == "top" else b.lo_idx),
                      # 但要知道"这里有个分型"，必须等右邻那根走完
                      confirmed_at=c.confirmed_at))
    return out


# ---------------------------------------------------------------------------
# 3. 笔
# ---------------------------------------------------------------------------
def build_bis(fxs: list[Fx], min_gap: int = MIN_BI_GAP) -> list[Bi]:
    """贪心 + 回退：维护交替端点，同类取更极端，异类够距才成笔。

    confirmed_at 取**下一笔起点分型的确认时间**。
    原因：一笔在它结束的那一刻并不能确认自己结束了——后面完全可能出现
    更低的底把它接上。只有当下一个异向分型被确认、形成新端点，
    这笔才算封口。因此最后一笔永远是"未确认"状态，这是事实而不是缺陷。
    """
    if len(fxs) < 2:
        return []
    eps: list[Fx] = [fxs[0]]
    for fx in fxs[1:]:
        last = eps[-1]
        if fx.kind == last.kind:
            more = (fx.value > last.value) if last.kind == "top" else (fx.value < last.value)
            if more:
                eps[-1] = fx
            continue
        gap = fx.m_idx - last.m_idx
        valid = (fx.value > last.value) if last.kind == "bottom" else (fx.value < last.value)
        if gap >= min_gap and valid:
            eps.append(fx)
            continue
        # 间距不够：只有它突破了上一个同类端点（说明上一个是假转折）才回退
        if len(eps) >= 2:
            prev_same = eps[-2]
            better = (fx.value > prev_same.value) if prev_same.kind == "top" else (fx.value < prev_same.value)
            if better:
                eps.pop()
                eps[-1] = fx
    bis: list[Bi] = []
    for k, (a, b) in enumerate(zip(eps, eps[1:])):
        if a.kind == b.kind:
            continue
        # 第 k 笔封口于第 k+1 笔起点分型的确认时点；最后一笔用自身确认时点
        closed = eps[k + 1].confirmed_at if k + 1 < len(eps) else b.confirmed_at
        bis.append(Bi(idx=len(bis), direction="up" if a.kind == "bottom" else "down",
                      a=a, b=b, high=max(a.high, b.high), low=min(a.low, b.low),
                      raw_start=a.raw_idx, raw_end=b.raw_idx,
                      confirmed_at=max(closed, b.confirmed_at)))
    return bis


# ---------------------------------------------------------------------------
# 4. 中枢
# ---------------------------------------------------------------------------
def find_zhongshus(bis: list[Bi]) -> list[ZS]:
    """连续三笔有公共重叠即成中枢；持续重叠则延伸，重叠消失则结束。

    ZG = min(各段高点)、ZD = max(各段低点)，这是缠论课 18 的原始公式。
    注意中枢的 confirmed_at 只在**第三笔封口**时成立——此时区间已经可用，
    后续延伸只会收窄而不会扩大，所以用成立时的区间做判断不存在偷看。
    """
    out: list[ZS] = []
    n = len(bis)
    i = 0
    while i + 2 < n:
        zg = min(bis[i].high, bis[i + 1].high, bis[i + 2].high)
        zd = max(bis[i].low, bis[i + 1].low, bis[i + 2].low)
        if zg <= zd:
            i += 1
            continue
        gg = max(bis[i].high, bis[i + 1].high, bis[i + 2].high)
        dd = min(bis[i].low, bis[i + 1].low, bis[i + 2].low)
        j = i + 3
        while j < n:
            nzg, nzd = min(zg, bis[j].high), max(zd, bis[j].low)
            if nzg > nzd:
                zg, zd = nzg, nzd
                gg, dd = max(gg, bis[j].high), min(dd, bis[j].low)
                j += 1
            else:
                break
        out.append(ZS(idx=len(out), ZG=zg, ZD=zd, GG=gg, DD=dd,
                      bi_from=i, bi_to=j - 1,
                      confirmed_at=bis[i + 2].confirmed_at,
                      closed_at=(bis[j].confirmed_at if j < n else None)))
        i = j
    return out


# ---------------------------------------------------------------------------
# 5. 背驰
# ---------------------------------------------------------------------------
def macd_series(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    """MACD 柱（与行情软件同口径：2×(DIF-DEA)）。"""
    c = pd.to_numeric(close, errors="coerce")
    ef = c.ewm(span=fast, adjust=False).mean()
    es = c.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def _area(hist: np.ndarray, start: int, end: int, up: bool) -> float:
    """某段同向 MACD 柱面积。只累计与方向同号的柱子。"""
    seg = hist[start:end + 1]
    if seg.size == 0:
        return 0.0
    return float(np.clip(seg if up else -seg, 0, None).sum())


# ---------------------------------------------------------------------------
# 6. 买卖点
# ---------------------------------------------------------------------------
def _relevant_zs(zss: list[ZS], a: Bi, c: Bi, going_up: bool) -> ZS | None:
    """背驰段是否离开了某个中枢（趋势背驰的必要条件）。"""
    for z in zss:
        if not (a.raw_start <= (z.closed_at or 10 ** 9) and z.confirmed_at <= c.raw_end):
            continue
        if going_up and c.b.value > z.ZG:
            return z
        if (not going_up) and c.b.value < z.ZD:
            return z
    return None


def _is_trend(zss: list[ZS], z: ZS, going_up: bool) -> bool:
    """中枢之前是否还有同向排列的中枢——有才是趋势背驰（一买/一卖）。"""
    i = z.idx
    if i <= 0:
        return False
    prev = zss[i - 1]
    return (z.ZD > prev.ZG) if going_up else (z.ZG < prev.ZD)


def find_signals(bis: list[Bi], zss: list[ZS], hist: np.ndarray,
                 beichi_ratio: float = BEICHI_RATIO) -> list[Signal]:
    """三类买卖点 + 盘整背驰。

    背驰判据（课 24）：相邻两个同向笔，后笔创新极值但 MACD 面积明显更小。
    "创新极值"必须有，否则只是回调不是背驰；"面积更小"是力度衰减的代理。
    """
    sig: list[Signal] = []
    for k in range(2, len(bis)):
        a, c = bis[k - 2], bis[k]
        if a.direction != c.direction:
            continue
        up = c.direction == "up"
        new_extreme = (c.b.value > a.b.value) if up else (c.b.value < a.b.value)
        if not new_extreme:
            continue
        z = _relevant_zs(zss, a, c, up)
        if z is None:
            continue
        area_a = _area(hist, a.raw_start, a.raw_end, up)
        area_c = _area(hist, c.raw_start, c.raw_end, up)
        if area_a <= 0 or area_c >= area_a * beichi_ratio:
            continue
        trend = _is_trend(zss, z, up)
        kind = ("sell1" if up else "buy1") if trend else ("pzsell" if up else "pzbuy")
        sig.append(Signal(kind=kind, bi_idx=k, price=c.b.value,
                          confirmed_at=c.confirmed_at,
                          reason=("趋势" if trend else "盘整") + ("顶背驰" if up else "底背驰"),
                          ratio=round(area_c / area_a, 3)))

    # 三买/三卖：离开中枢后，回抽不再回到中枢区间
    for z in zss:
        leave = None
        for i in range(z.bi_to + 1, len(bis)):
            u = bis[i]
            if u.direction == "up" and u.b.value > z.ZG:
                leave, up = i, True
                break
            if u.direction == "down" and u.b.value < z.ZD:
                leave, up = i, False
                break
        if leave is None:
            continue
        for i in range(leave + 1, len(bis)):
            u = bis[i]
            if up:
                if u.direction != "down":
                    continue
                if u.b.value > z.ZG:
                    sig.append(Signal("buy3", i, u.b.value, u.confirmed_at,
                                      f"离开中枢后回抽不破上沿 ZG={z.ZG:.2f}"))
                break
            if u.direction != "up":
                continue
            if u.b.value < z.ZD:
                sig.append(Signal("sell3", i, u.b.value, u.confirmed_at,
                                  f"跌破中枢后反抽不上沿 ZD={z.ZD:.2f}"))
            break

    sig.sort(key=lambda s: s.confirmed_at)
    return sig


# ---------------------------------------------------------------------------
def analyze(df: pd.DataFrame, min_gap: int = MIN_BI_GAP) -> ChanResult:
    """一次算完：结构 + 信号。df 需含 high/low/close，索引为 0..n-1。"""
    r = ChanResult()
    if df is None or len(df) < 60:
        return r
    r.bars = merge_bars(df)
    r.fractals = find_fractals(r.bars)
    r.bis = build_bis(r.fractals, min_gap=min_gap)
    r.zhongshus = find_zhongshus(r.bis)
    _, _, hist = macd_series(df["close"])
    r.signals = find_signals(r.bis, r.zhongshus, hist.to_numpy(dtype=float))
    return r


# ---------------------------------------------------------------------------
# 对外摘要：给 payload 用，不参与概率
# ---------------------------------------------------------------------------
BUY_KINDS = {"buy1", "buy2", "buy3", "pzbuy"}
SELL_KINDS = {"sell1", "sell2", "sell3", "pzsell"}


def _fwd(df: pd.DataFrame, i: int, n: int) -> float | None:
    """确认日次日开盘入场、持有 n 日的收益（%）。"""
    e = i + 1
    if e >= len(df):
        return None
    entry = float(df["open"].iloc[e])
    j = e + n - 1
    if j >= len(df) or not np.isfinite(entry) or entry <= 0:
        return None
    return (float(df["close"].iloc[j]) / entry - 1) * 100


def _side_stats(df: pd.DataFrame, idx: list[int], n: int) -> dict:
    vals = [_fwd(df, i, n) for i in idx]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    a = np.array(vals)
    return {"n": int(a.size), "mean": round(float(a.mean()), 2),
            "win": round(float((a > 0).mean()), 3)}


def summarize(df: pd.DataFrame, recent_days: int = 10) -> dict:
    """把缠论结构压成前端能直接读的一小块。

    样本量必须一起给出去。24 个信号、14 个买点，任何"胜率 86%"的结论
    都必须带上这个 n，否则读者会自动把它当成一个可靠的胜率。
    """
    if df is None or len(df) < 120:
        return {}
    df = df.reset_index(drop=True)
    r = analyze(df)
    if not r.bis:
        return {}

    last = r.bis[-1]
    out: dict = {
        "bis": len(r.bis),
        "zhongshus": len(r.zhongshus),
        "last_bi": {
            "dir": last.direction,
            "from": str(df["date"].iloc[last.raw_start])[:10],
            "to": str(df["date"].iloc[last.raw_end])[:10],
            "high": round(float(last.high), 2),
            "low": round(float(last.low), 2),
            # 最后一笔永远未封口——它还在延伸
            "confirmed": bool(last.confirmed_at < len(df) - 1),
        },
        "signals_total": len(r.signals),
        "buy": _side_stats(df, [s.confirmed_at for s in r.signals if s.kind in BUY_KINDS], 3),
        "sell": _side_stats(df, [s.confirmed_at for s in r.signals if s.kind in SELL_KINDS], 3),
    }
    if r.zhongshus:
        z = r.zhongshus[-1]
        zs_from = ""
        if 0 <= z.bi_from < len(r.bis):
            zs_from = str(df["date"].iloc[r.bis[z.bi_from].raw_start])[:10]
        out["last_zs"] = {"ZG": round(float(z.ZG), 2), "ZD": round(float(z.ZD), 2),
                          "from": zs_from,
                          # closed_at 为空 = 这个中枢还在延伸，区间可能继续收窄
                          "confirmed": z.closed_at is not None}

    # 最近一次信号：只在它还很"新"的时候提示，过期的不提
    if r.signals:
        s = r.signals[-1]
        age = len(df) - 1 - s.confirmed_at
        if age <= recent_days:
            out["recent"] = {"kind": s.kind, "date": str(df["date"].iloc[s.confirmed_at])[:10],
                             "price": round(float(s.price), 2), "reason": s.reason,
                             "age": int(age),
                             "is_buy": s.kind in BUY_KINDS}
    return out
