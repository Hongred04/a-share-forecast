"""复盘层：收盘后把预判与真实走势逐项比对，做误差归因。

拆成跳空 / 日内 / 全天三类误差，因为它们的可归因性完全不同。

"""
from __future__ import annotations

from . import analytics as A
from . import config as C
from . import datahub as D
import json
import numpy as np
import pandas as pd
from .store import (db, load_learner, save_learner, save_stock_calib)

def _stock_secid(code: str) -> str:
    code = str(code).strip()
    return f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"

def _rank_corr(a: list[float], b: list[float]) -> float:
    if len(a) < 5 or len(a) != len(b):
        return 0.0
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])

def evaluate_target(target: str, mkt: dict | None = None) -> dict | None:
    """用 target 日真实数据评价该日预判，并完成一次复盘/学习。"""
    with db() as c:
        row = c.execute("SELECT * FROM pred WHERE target=? AND evaluated=0", (target,)).fetchone()
    if row is None:
        return None
    pl = json.loads(row["payload"])
    tgt = pd.Timestamp(target)

    bench = (mkt or {}).get("bench_raw")
    if bench is None or bench.empty:
        bench = D.kline(C.INDEXES[C.BENCHMARK])
    bench = bench.reset_index(drop=True)
    pos = bench.index[bench["date"] == tgt]
    if len(pos) == 0 or pos[0] == 0:
        return None
    i = int(pos[0])
    cur, prev = bench.iloc[i], bench.iloc[i - 1]

    def _f(v):
        """把可能为 None / NaN / 字符串的字段安全转成 float，失败返回 None。

        不能直接 float(v)：备用数据源曾经把整列 pct 留成 None（object dtype），
        float(None) 抛 TypeError，连带整个 build 失败、快照写不出来。
        评价单条预判失败可以容忍（下次再评），把主流程拖垮不行。
        """
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if np.isfinite(x) else None

    c_cur, c_prev = _f(cur.get("close")), _f(prev.get("close"))
    o_cur = _f(cur.get("open"))

    actual = _f(cur.get("pct"))
    if actual is None and c_cur is not None and c_prev not in (None, 0.0):
        # 数据源没给涨跌幅不等于没法评价：有收盘价就够了。
        actual = (c_cur / c_prev - 1) * 100
    if actual is None:
        return None

    if o_cur is not None and c_prev not in (None, 0.0):
        actual_gap = (o_cur / c_prev - 1) * 100
    else:
        actual_gap = 0.0
    if o_cur not in (None, 0.0) and c_cur is not None:
        actual_intra = (c_cur / o_cur - 1) * 100
    else:
        # 拆不开隔夜/日内时，明确把日内记成全天、跳空记成 0，
        # 并在纠错记录里体现为拆分缺失——不编一个中间值出来。
        actual_gap, actual_intra = 0.0, actual

    y = 1 if actual > 0 else 0
    yi = 1 if actual_intra > 0 else 0
    hit = int((pl["prob_up"] > 0.5) == bool(y))
    gap_hit = int((pl.get("p_gap_up", 0.5) > 0.5) == (actual_gap > 0))
    brier = float((pl["prob_up"] - y) ** 2)

    # ---- 日内分量：在线学习一步 ----
    signals = [A.Signal(s["key"], s["label"], s["score"], s["detail"]) for s in pl["signals"]]
    learner = load_learner()
    w_old = dict(learner.w)
    info = learner.update(signals, yi)
    save_learner(learner, day=pl["asof"])
    w_new = dict(learner.w)

    rows = [(pl["asof"], s["key"], float(s["score"]), y, yi,
             1 if s["key"] in A.LIVE_ONLY else 0)
            for s in pl["signals"] if s["available"]]
    if rows:
        with db() as c:
            c.executemany(
                "INSERT INTO sig_log(asof,key,score,y,yi,live) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(asof,key) DO UPDATE SET score=excluded.score,y=excluded.y,"
                "yi=excluded.yi", rows)
    with db() as c:
        c.execute("INSERT INTO day_log(asof,ov,gap,intra,c2c) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(asof) DO UPDATE SET ov=excluded.ov,gap=excluded.gap,"
                  "intra=excluded.intra,c2c=excluded.c2c",
                  (pl["asof"], float(pl.get("ov_pct", 0.0)), actual_gap, actual_intra, actual))

    # ---- 板块 IC ----
    with db() as c:
        srows = [dict(r) for r in c.execute(
            "SELECT code,z FROM sector_pred WHERE target=?", (target,)).fetchall()]
    sector_ic, s_updates = 0.0, []
    if srows and mkt is not None and mkt.get("sec_frames"):
        zs, rs = [], []
        for r in srows:
            f = mkt["sec_frames"].get(r["code"])
            if f is None or f.empty:
                continue
            j = A.at_index(f, tgt)
            if j < 0 or pd.Timestamp(f["date"].iloc[j]) != tgt:
                continue
            act = float(f["r1"].iloc[j])
            zs.append(float(r["z"]))
            rs.append(act)
            s_updates.append((act, target, r["code"]))
        sector_ic = _rank_corr(zs, rs) if len(zs) >= 5 else 0.0
    if s_updates:
        with db() as c:
            c.executemany("UPDATE sector_pred SET actual_pct=? WHERE target=? AND code=?",
                          s_updates)

    # ---- 个股（统一取目标日真实涨跌） ----
    with db() as c:
        prows = [dict(r) for r in c.execute(
            "SELECT code, prob, prob_raw FROM stock_pred WHERE target=?", (target,)).fetchall()]
    stock_hits = stock_n = 0
    st_updates = []
    for r in prows:
        # 必须取 >=20 根：kline() 内部有 `len(out) >= 20` 的健全性门槛，
        # 少于 20 根会被当成"源返回残缺"直接丢弃、返回空表。
        # 原来这里传 12，于是每一次个股评价都静默拿到空表 →
        # stock_pred 攒了 52 行、actual_pct 永远是 NULL，
        # 个股概率校准因此从未有过一个可用样本。取 40 根留出停牌余量。
        k = D.kline(_stock_secid(r["code"]), 40)
        if k.empty:
            continue
        sub = k[k["date"] == tgt]
        if sub.empty:
            continue
        ap = float(sub.iloc[-1]["pct"])
        st_updates.append((ap, target, r["code"]))
        stock_n += 1
        stock_hits += int(ap > 0)
    if st_updates:
        with db() as c:
            c.executemany("UPDATE stock_pred SET actual_pct=? WHERE target=? AND code=?",
                          st_updates)

    # ---- 个股概率校准：只能用"已评价的真实样本"拟合 ----
    # 两个硬约束：
    # 1) 只用 prob_raw，绝不用 prob。prob 已是校准层输出，拿输出再拟合一遍
    #    会自我叠加，几轮后所有个股被压到 0.5 附近，而 Brier 反而"更好看"。
    # 2) 只用 prob_raw 非空的行。加这个字段之前选出的老记录，概率口径跟现在
    #    完全不同（实测 0.41~0.70 分散，现在挤在 0.655~0.682），混进拟合
    #    会把两种分布当成同一个模型的历史成绩，参数被拖向错误方向。
    stock_calib_info = None
    with db() as c:
        crows = [dict(r) for r in c.execute(
            "SELECT prob_raw, actual_pct FROM stock_pred "
            "WHERE actual_pct IS NOT NULL AND prob_raw IS NOT NULL "
            "ORDER BY target DESC LIMIT 1200").fetchall()]
    if crows:
        fit = A.fit_stock_calib(crows)
        if fit is not None:
            # 只有确实降低了 Brier 才落库。improved=False 说明拟合没找到
            # 比"原样输出"更好的参数，这时覆盖掉旧参数等于用一个更差的解
            # 顶替现有解——校准应该是单调变好的，不能来回抖。
            if fit.get("improved"):
                save_stock_calib(fit)
                stock_calib_info = fit

    with db() as c:
        c.execute("UPDATE pred SET actual_pct=?,actual_dir=?,hit=?,brier=?,evaluated=1,"
                  "actual_gap=?,actual_intra=?,gap_hit=? WHERE target=?",
                  (actual, "上涨" if actual > 0 else "下跌", hit, brier,
                   actual_gap, actual_intra, gap_hit, target))

    diag, action = diagnose(pl, actual, actual_gap, actual_intra, hit, gap_hit,
                            sector_ic, w_old, w_new)
    with db() as c:
        c.execute("INSERT INTO errors(day,target,kind,detail,diagnosis,action) "
                  "VALUES(?,?,?,?,?,?)",
                  (pl["asof"], target, "跳空" if not gap_hit else ("方向" if not hit else "幅度"),
                   f"预判{e_gap_text(pl)}，实际跳空{actual_gap:+.2f}%、日内{actual_intra:+.2f}%、"
                   f"全天{actual:+.2f}%", diag, action))

    return {"target": target, "actual": actual, "actual_gap": actual_gap,
            "actual_intra": actual_intra, "hit": hit, "gap_hit": gap_hit,
            "brier": brier, "sector_ic": sector_ic,
            "stock_hits": stock_hits, "stock_n": stock_n,
            "stock_calib": stock_calib_info,
            "weights": w_new, "dw": info["dw"], "diagnosis": diag, "action": action}

def e_gap_text(pl: dict) -> str:
    return (f"跳空{pl.get('e_gap', 0):+.2f}%（开盘方向概率{pl.get('p_gap_up', 0.5):.0%}）、"
            f"日内{pl.get('e_intra', 0):+.2f}%，全天{pl.get('exp_ret', 0):+.2f}%"
            f"[{pl.get('exp_lo', 0):+.2f}%,{pl.get('exp_hi', 0):+.2f}%]")

def diagnose(pl: dict, actual: float, a_gap: float, a_intra: float, hit: int, gap_hit: int,
             sector_ic: float, w_old: dict, w_new: dict) -> tuple[str, str]:
    """误差归因：只记录可核对的事实——偏差值与权重变化。

    旧版本每条都附一句"说明隔夜外盘的传导系数当日失真"、"将在下次重估时重算"，
    那是模板而不是信息：重复几百次不产生任何新内容，还把真正要看的数字淹掉了。
    信号层面的对错可以在「自进化」的权重表里看到，不需要在这里复述。
    """
    label = A.EXPERT_LABELS
    s_intra = float(pl.get("s_intra", 1.0) or 1.0)
    err_gap = a_gap - float(pl.get("e_gap", 0))
    err_intra = a_intra - float(pl.get("e_intra", 0))

    facts: list[str] = []
    if not gap_hit:
        facts.append(f"跳空{err_gap:+.2f}pct")
    if abs(err_intra) > 0.9 * s_intra:
        facts.append(f"日内{err_intra:+.2f}pct")
    if abs(sector_ic) > 0.02:
        facts.append(f"板块IC{sector_ic:+.2f}")

    moves = sorted(((w_new[k] - w_old.get(k, 0), k) for k in w_new), key=lambda x: -abs(x[0]))
    moved = [f"{label[k]}{v:+.3f}" for v, k in moves[:2] if abs(v) > 5e-4]
    if moved:
        facts.append("权重 " + " ".join(moved))

    return " · ".join(facts) or "无误", ""