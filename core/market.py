"""市场装配层：把分散的行情端点拼成一次预判所需的完整快照。

包含交易时段判断、ETF 映射与份额、全市场广度与资金、新闻情绪。
输出的 dict 就是 `predict` 层的输入契约。

"""
from __future__ import annotations

from . import analytics as A
from . import config as C
from . import datahub as D
import datetime as dt
import numpy as np
import pandas as pd
import time
from .store import (db)

def _phase() -> str:
    return D.market_phase()

def _elapsed_fraction() -> float:
    """A 股日内成交量的累计分布（U 型），把盘中成交额折算为全日当量。"""
    now = dt.datetime.now()
    hm = now.hour * 60 + now.minute
    knots = [(9 * 60 + 30, 0.0), (10 * 60, 0.20), (10 * 60 + 30, 0.32),
             (11 * 60, 0.42), (11 * 60 + 30, 0.50), (13 * 60, 0.50),
             (13 * 60 + 30, 0.60), (14 * 60, 0.70), (14 * 60 + 30, 0.82),
             (15 * 60, 1.0)]
    if hm <= knots[0][0]:
        return 0.02
    if hm >= knots[-1][0]:
        return 1.0
    for (t0, v0), (t1, v1) in zip(knots, knots[1:]):
        if t0 <= hm <= t1:
            return max(v0 + (v1 - v0) * (hm - t0) / max(t1 - t0, 1), 0.02)
    return 1.0

def _is_live_bar(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False
    last = pd.Timestamp(df.iloc[-1]["date"]).normalize()
    if last != pd.Timestamp.today().normalize():
        return False
    return _phase() in ("盘前", "集合竞价", "盘中", "午间休市")

def _num(v, d: float = 0.0) -> float:
    return float(v) if v is not None and np.isfinite(v) else d

def build_etf_map(sector_names: list[str], etf: pd.DataFrame) -> dict[str, dict]:
    """给每个板块找一只成交额最大的对应 ETF，作为该板块的资金观察窗口。"""
    if etf is None or etf.empty:
        return {}
    names = etf["name"].astype(str).to_numpy()
    amounts = pd.to_numeric(etf["amount"], errors="coerce").fillna(0).to_numpy()
    out: dict[str, dict] = {}
    for sec in sector_names:
        cands = None
        # 长关键词优先："电力设备"必须先于"电力"匹配，否则会被错配到电力公用事业ETF
        for key in sorted(C.SECTOR_ETF_KEYWORDS, key=len, reverse=True):
            if key in str(sec):
                cands = C.SECTOR_ETF_KEYWORDS[key]
                break
        if not cands:
            continue
        best_i, best_amt = -1, -1.0
        for frag in cands:
            mask = np.array([frag in n for n in names])
            if not mask.any():
                continue
            idx = np.where(mask)[0]
            j = int(idx[np.argmax(amounts[idx])])
            if amounts[j] > best_amt:
                best_i, best_amt = j, float(amounts[j])
        if best_i < 0:
            continue
        r = etf.iloc[best_i]
        out[sec] = {
            "code": str(r["code"]), "name": str(r["name"]),
            "pct": round(_num(r.get("pct")), 2),
            "amount_yi": round(_num(r.get("amount")) / 1e8, 2),
            "net_yi": round(_num(r.get("net_in")) / 1e8, 2),
            "net_ratio": round(_num(r.get("net_ratio")), 2),
            "share_chg": None,
        }
    return out

def record_etf_shares(etf: pd.DataFrame, day: str) -> None:
    """每日记录 ETF 份额，用于计算申购赎回（份额增 = 真实资金流入）。"""
    if etf is None or etf.empty:
        return
    rows = [(day, str(r["code"]), float(r["shares"]))
            for _, r in etf.iterrows() if pd.notna(r.get("shares")) and r["shares"] > 0]
    if not rows:
        return
    with db() as c:
        c.executemany("INSERT INTO etf_share(day,code,shares) VALUES(?,?,?) "
                      "ON CONFLICT(day,code) DO UPDATE SET shares=excluded.shares", rows)

def etf_share_changes(max_days: int = 5) -> dict[str, float]:
    """近 N 个记录日的份额变化率（%）。样本不足时返回空。"""
    with db() as c:
        days = [r["day"] for r in c.execute(
            "SELECT DISTINCT day FROM etf_share ORDER BY day DESC LIMIT ?", (max_days,)).fetchall()]
    if len(days) < 2:
        return {}
    old, new = min(days), max(days)
    with db() as c:
        rows = c.execute(
            "SELECT a.code, a.shares AS s0, b.shares AS s1 FROM etf_share a "
            "JOIN etf_share b ON a.code=b.code WHERE a.day=? AND b.day=?", (old, new)).fetchall()
    out = {}
    for r in rows:
        if r["s0"] and r["s0"] > 0:
            out[r["code"]] = float((r["s1"] / r["s0"] - 1) * 100)
    return out

def load_market() -> dict:
    t0 = time.time()
    # 每轮构建从干净状态开始记降级：这个字典是进程级单例，
    # 不清空的话上一次的"某板块没拉到"会一直挂在界面上，
    # 变成一条永远不消失的告警——比不报还糟。
    D.FALLBACK_USED["kline"] = False
    D.FALLBACK_USED["sector_members"] = []
    D.FALLBACK_USED["etf"] = False
    D.FALLBACK_USED["stock_spot"] = None
    idx_raw = D.index_klines()
    bench_raw = idx_raw.get(C.BENCHMARK, pd.DataFrame())
    if bench_raw.empty:
        raise RuntimeError("行情数据拉取失败：无法获取上证指数日线，请检查网络后重试")
    live_bar = bench_raw.iloc[-1].to_dict() if _is_live_bar(bench_raw) else None

    # 用真实交易日反推休市日：硬编码表总会过期，日线不会。
    # 放在这里是因为这是全流程唯一"刚拿到权威交易日序列"的位置。
    try:
        D.sync_from_kline(bench_raw["date"])
    except Exception:  # noqa: BLE001  日历只是辅助，补不上不能影响主流程
        pass

    bench = A.enrich(bench_raw)
    panel = A.align_panel(idx_raw, "pct")

    ov_raw = D.overseas_klines()
    ov_enriched = {k: A.enrich(v) for k, v in ov_raw.items()}
    ov_ret = {k: float(v.iloc[-1]["r1"]) for k, v in ov_enriched.items()
              if v is not None and not v.empty}

    leaders = D.overseas_leaders()
    leader_pct = float(leaders["pct"].mean()) if not leaders.empty else None

    sec = D.sector_list("industry")
    sec_spot = D.sector_list("concept")
    frames_ = [f for f in (sec, sec_spot) if f is not None and not f.empty]
    sec_all = pd.concat(frames_, ignore_index=True) if frames_ else pd.DataFrame()

    if sec_all.empty:
        pool_codes, names, sec_frames = [], {}, {}
        pool = pd.DataFrame()
    else:
        pool = sec_all.dropna(subset=["pct"]).copy()
        pool = pool[pool["code"].notna()]
        pool = pool[~pool["name"].astype(str).str.contains("|".join(C.SECTOR_BLACKLIST), na=False)]
        pool["amount"] = pd.to_numeric(pool.get("amount"), errors="coerce").fillna(0)
        pool["mcap"] = pd.to_numeric(pool.get("mcap"), errors="coerce").fillna(0)
        pool = pool[pool["amount"] > C.SECTOR_MIN_AMOUNT]
        pool = pool[pool["mcap"] > 8e9]
        pool = pool.drop_duplicates(subset=["code"])
        pool = pool.sort_values("amount", ascending=False).head(C.SECTOR_POOL_SIZE)
        pool_codes = pool["code"].tolist()
        names = dict(zip(pool["code"], pool["name"]))
        kl = D.sector_klines(pool_codes)
        sec_frames = {}
        for code, df in kl.items():
            e = A.enrich(df)
            if e.empty:
                continue
            e["name"] = names.get(code, code)
            sec_frames[code] = e

    # 盘中的量能折算：把"截至当前的成交额"外推为全日当量
    frac = _elapsed_fraction()
    intraday = live_bar is not None and 0.02 < frac < 0.995
    if intraday:
        today = pd.Timestamp.today().normalize()
        if not bench.empty and pd.Timestamp(bench.iloc[-1]["date"]).normalize() == today:
            i = bench.index[-1]
            base = bench.loc[i, "amt20"]
            if pd.notna(base) and base > 0:
                bench.loc[i, "vol_ratio_amt"] = float(bench.loc[i, "amount"]) / frac / float(base)
        for f in sec_frames.values():
            if f.empty or pd.Timestamp(f.iloc[-1]["date"]).normalize() != today:
                continue
            i = f.index[-1]
            base = f.loc[i, "amt20"]
            if pd.notna(base) and base > 0:
                f.loc[i, "vol_ratio_amt"] = float(f.loc[i, "amount"]) / frac / float(base)

    spot = D.stock_spot()
    if intraday and spot is not None and not spot.empty:
        spot = spot.copy()
        spot["amount"] = pd.to_numeric(spot["amount"], errors="coerce") / frac
    limit = D.limit_stats(dt.date.today())
    news = D.news(50)

    # ---- ETF：板块资金的第二观察窗口 ----
    etf = D.etf_spot()
    if etf is not None and not etf.empty:
        etf = etf[etf["amount"] >= C.ETF_MIN_AMOUNT].copy()
        try:
            record_etf_shares(etf, pd.Timestamp.today().strftime("%Y-%m-%d"))
        except Exception:  # noqa: BLE001
            pass
    # ---- 两融 / ETF 份额 / 尾盘：护盘层的三个数据源 ----
    # 两融走 datacenter（与 push2 限流独立），份额走 f10 季度口径，
    # 尾盘只能拿到近 32 个交易日的 5 分钟线。三者都带磁盘缓存。
    margin = pd.DataFrame()
    try:
        margin = D.margin_history(320)
    except Exception:  # noqa: BLE001
        pass
    share_hist: dict[str, pd.DataFrame] = {}
    for _code in ("510300", "510050", "510500", "588000"):
        try:
            _h = D.etf_share_hist(_code)
            if _h is not None and not _h.empty:
                share_hist[_code] = _h
        except Exception:  # noqa: BLE001
            continue
    tail30 = pd.DataFrame()
    try:
        tail30 = D.tail30_series()
    except Exception:  # noqa: BLE001
        pass

    etf_map = build_etf_map(sorted(set(names.values())), etf)
    chg = etf_share_changes()
    for _v in etf_map.values():
        if _v["code"] in chg:
            _v["share_chg"] = round(float(chg[_v["code"]]), 2)

    return {
        "loaded_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": _phase(),
        "bench_raw": bench_raw,
        "bench": bench,
        "panel": panel,
        "ov_raw": ov_raw,
        "ov_ret": ov_ret,
        "leaders": leaders,
        "leader_pct": leader_pct,
        "sector_spot": sec,
        "sector_spot_c": sec_spot,
        "sector_pool": pool,
        "sec_frames": sec_frames,
        "spot": spot,
        "limit": limit,
        "news": news,
        "etf": etf,
        "etf_map": etf_map,
        "margin": margin,
        "etf_share_hist": share_hist,
        "tail30": tail30,
        "live_bar": live_bar,
        "elapsed": round(time.time() - t0, 1),
    }

def compute_breadth(spot: pd.DataFrame, limit: dict) -> dict:
    if spot is None or spot.empty:
        return {}
    pct = spot["pct"].dropna()
    amt = spot["amount"].fillna(0)
    big = spot[amt > 5e7]
    return {
        "n": int(len(pct)),
        "up_ratio": float((pct > 0).mean()),
        "median_pct": float(pct.median()),
        "mean_pct": float(pct.mean()),
        "limit_up": int(limit.get("limit_up", 0)),
        "limit_down": int(limit.get("limit_down", 0)),
        "big_up_ratio": float((big["pct"] > 0).mean()) if not big.empty else 0.5,
    }

def compute_capital(spot: pd.DataFrame) -> dict:
    if spot is None or spot.empty or "net_in" not in spot:
        return {}
    # 整列都是 NaN 说明数据源根本不提供主力净流入（例如新浪备用源）。
    # 这时候 fillna(0) 会算出"净流入 0 亿、流入占比 0%"，
    # 界面上就是一个看起来很正常、实际毫无信息的 0——
    # 读者会把它理解成"今天资金没动"，而不是"这条数据没有"。
    if spot["net_in"].notna().sum() == 0:
        return {"unavailable": True}
    net = spot["net_in"].fillna(0)
    amt = spot["amount"].fillna(0)
    total_amt = float(amt.sum()) or 1.0
    return {"net_yi": float(net.sum() / 1e8),
            "net_ratio": float(net.sum() / total_amt),
            "pos_share": float((net > 0).mean()),
            "top_net_yi": float(net.nlargest(20).sum() / 1e8),
            "unavailable": False}

def news_sentiment(items: list[dict]) -> tuple[float | None, list[dict]]:
    if not items:
        return None, []
    scored, tot = [], 0.0
    for it in items:
        t = it.get("title", "")
        pos = sum(t.count(w) for w in C.POS_WORDS)
        neg = sum(t.count(w) for w in C.NEG_WORDS)
        s = (pos - neg) / (pos + neg + 2.0)
        tot += s
        scored.append({"title": t, "time": it.get("time", ""), "score": round(s, 2)})
    score = float(np.clip(tot / (len(items) ** 0.5) * 0.35, -3, 3))
    return score, sorted(scored, key=lambda x: -abs(x["score"]))[:12]

def sector_live_map(sec_spot: pd.DataFrame, sec_spot_c: pd.DataFrame) -> dict:
    frames = [f for f in (sec_spot, sec_spot_c) if f is not None and not f.empty]
    if not frames:
        return {}
    allsec = pd.concat(frames, ignore_index=True)
    live = {}
    for _, r in allsec.iterrows():
        nr = r.get("net_ratio")
        uc = r.get("up_cnt")
        dc = r.get("down_cnt")
        uc = float(uc) if uc is not None and pd.notna(uc) else 0.0
        dc = float(dc) if dc is not None and pd.notna(dc) else 0.0
        live[r["code"]] = {"net_ratio": float(nr) if nr is not None and pd.notna(nr) else 0.0,
                           "up_ratio": uc / max(uc + dc, 1.0)}
    return live

