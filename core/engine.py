"""编排层：日常闭环、历史回放、对外状态组装。

模块分工（按依赖方向，下层不依赖上层）
    store    SQLite 建表/迁移、元数据、学习器与预判结果读写
    market   行情装配：交易时段、ETF、广度与资金、新闻情绪
    model    上下文构建、跳空/日内模型、概率与区间校准
    predict  合成预判 payload
    review   收盘复盘与误差归因
    engine   回放、日常闭环、状态组装

本文件对外仍暴露全部历史函数名（下方 re-export），
因此 `from core import engine as E` 的旧调用无需改动。
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd

from . import analytics as A
from . import config as C
from . import datahub as D
from . import depth as DP
from . import rescue as RS

from .store import (  # noqa: F401  —— 门面：保持旧调用兼容
    SCHEMA, MIGRATIONS, DEFAULT_GAP, DEFAULT_INTRA, db, init_db, meta_get, meta_set, load_learner, save_learner, load_weights, gap_model, intra_model, save_prediction,
)
from .market import (  # noqa: F401  —— 门面：保持旧调用兼容
    _phase, _elapsed_fraction, _is_live_bar, _num, build_etf_map, record_etf_shares, etf_share_changes, load_market, compute_breadth, compute_capital, news_sentiment, sector_live_map,
)
from .model import (  # noqa: F401  —— 门面：保持旧调用兼容
    bench_row_at, panel_breadth_at, sector_stat_at, overseas_for, ov_weighted, build_context, prob_calib, apply_calib, fit_prob_calib, gap_scenario, refit_models, interval_calib, fit_interval, fit_intra_edge, param_log, _log_params,
)
from .predict import (  # noqa: F401  —— 门面：保持旧调用兼容
    predict_payload,
)
from .review import (  # noqa: F401  —— 门面：保持旧调用兼容
    _stock_secid, _rank_corr, evaluate_target, e_gap_text, diagnose,
)


_DEPTH_CACHE: dict[str, dict] = {}

_RESCUE_CACHE: dict[str, dict] = {}


def rescue_payload(mkt: dict, asof: pd.Timestamp, refresh: bool = False) -> dict:
    """护盘/国家队共振快照。与深度层同口径：只用已收盘交易日。"""
    key = str(pd.Timestamp(asof).date())
    if not refresh and key in _RESCUE_CACHE:
        return _RESCUE_CACHE[key]
    try:
        r = RS.snapshot(mkt, asof)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {ex}"}
    _RESCUE_CACHE.clear()
    _RESCUE_CACHE[key] = r
    return r


def depth_payload(mkt: dict, asof: pd.Timestamp, pl: dict | None = None,
                  refresh: bool = False) -> dict:
    """深度层快照。只用到 asof 及之前的完整交易日，不含当日盘中快照。"""
    pred_gap = round(float(pl.get("e_gap", 0) or 0), 2) if pl else None
    # 键里带上预判跳空：同一交易日不同预判会落到不同条件桶，不能共用缓存
    key = f"{pd.Timestamp(asof).date()}|{pred_gap}"
    if not refresh and key in _DEPTH_CACHE:
        return _DEPTH_CACHE[key]
    bench = mkt["bench_raw"]
    b = bench[pd.to_datetime(bench["date"]).dt.normalize() <= pd.Timestamp(asof)]
    try:
        d = DP.snapshot(mkt, b, pred_gap=pred_gap)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{type(ex).__name__}: {ex}"}
    if pl:
        d["decision"] = DP.decide(pl, d)
    _DEPTH_CACHE.clear()
    _DEPTH_CACHE[key] = d
    return d

def replay(mkt: dict, days: int = C.REPLAY_DAYS, reset: bool = False) -> dict:
    bench = mkt["bench_raw"]
    dates = list(pd.to_datetime(bench["date"]).dt.normalize())
    complete = dates[:-1] if _is_live_bar(bench) else dates
    if len(complete) < 120:
        return {"ok": False, "msg": "历史数据不足"}
    window = complete[-days:]
    if reset:
        with db() as c:
            c.execute("DELETE FROM pred WHERE source='replay'")
            c.execute("DELETE FROM sector_pred")
            c.execute("DELETE FROM errors")
            c.execute("DELETE FROM sig_log")
            c.execute("DELETE FROM day_log")
            c.execute("DELETE FROM weight_hist")
        meta_set("learner", A.Learner().to_dict())
        meta_set("gap_model", None)
        meta_set("intra_model", None)

    results, hit_l, gap_l, brier_l, ic_l = [], [], [], [], []
    streak = max_streak = 0
    for i, asof in enumerate(window[:-1]):
        target = window[i + 1]
        try:
            pl = predict_payload(mkt, asof, target, source="replay")
        except Exception:  # noqa: BLE001
            continue
        save_prediction(pl)
        ev = evaluate_target(target.strftime("%Y-%m-%d"), mkt)
        if ev is None:
            continue
        hit_l.append(ev["hit"])
        gap_l.append(ev["gap_hit"])
        brier_l.append(ev["brier"])
        ic_l.append(ev["sector_ic"])
        streak = streak + 1 if ev["hit"] else 0
        max_streak = max(max_streak, streak)
        results.append({"asof": pl["asof"], "target": pl["target"],
                        "direction": pl["direction"], "prob_up": pl["prob_up"],
                        "exp_ret": pl["exp_ret"], "e_gap": pl["e_gap"],
                        "actual": round(ev["actual"], 2),
                        "actual_gap": round(ev["actual_gap"], 2),
                        "hit": ev["hit"], "gap_hit": ev["gap_hit"],
                        "ic": round(ev["sector_ic"], 3)})
        if (i + 1) % 5 == 0:
            refit_models()

    refit_models()
    n = len(hit_l)
    base = float(np.mean([r["actual"] > 0 for r in results])) if results else 0.0
    stats = {"ok": True, "n": n,
             "hit_rate": round(float(np.mean(hit_l)), 4) if n else 0.0,
             "gap_hit_rate": round(float(np.mean(gap_l)), 4) if n else 0.0,
             "base_rate": round(base, 4),
             "brier": round(float(np.mean(brier_l)), 4) if n else 0.0,
             "avg_ic": round(float(np.mean(ic_l)), 4) if n else 0.0,
             "ic_positive_rate": round(float(np.mean([1 if x > 0 else 0 for x in ic_l])), 4) if n else 0.0,
             "max_streak": int(max_streak),
             "window": [window[0].strftime("%Y-%m-%d"), window[-1].strftime("%Y-%m-%d")],
             "series": results[-90:]}
    meta_set("replay_stats", stats)
    meta_set("version", int(meta_get("version", 0) or 0) + 1)
    meta_set("last_replay", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return stats

def _eval_pending(mkt: dict) -> int:
    """把已收盘的目标日预判补做评价，返回评价条数。"""
    bench = mkt["bench_raw"].reset_index(drop=True)
    known = set(pd.to_datetime(bench["date"]).dt.normalize())
    with db() as c:
        pending = [dict(r) for r in c.execute(
            "SELECT target FROM pred WHERE evaluated=0 ORDER BY target").fetchall()]
    n = 0
    for p in pending:
        tgt = pd.Timestamp(p["target"])
        if tgt not in known:
            continue
        rows = bench[bench["date"] == tgt]
        if _is_live_bar(rows):      # 当日仍在交易中，尚不能评价
            continue
        if evaluate_target(p["target"], mkt) is not None:
            n += 1
    return n

def run_daily() -> dict:
    """日常运行：补评到期预判 → 重估 → 产出下一交易日预判。"""
    init_db()
    mkt = load_market()
    evaluated = _eval_pending(mkt)
    if evaluated:
        refit_models()
    return {"evaluated": evaluated}

def build_state(force_replay: bool = False) -> dict:
    init_db()
    mkt = load_market()
    bench = mkt["bench_raw"]
    all_dates = list(pd.to_datetime(bench["date"]).dt.normalize())
    complete = all_dates[:-1] if _is_live_bar(bench) else all_dates
    asof_closed = complete[-1] if complete else pd.Timestamp.today().normalize()

    stats = meta_get("replay_stats")
    if force_replay or not stats:
        stats = replay(mkt, reset=force_replay or not stats)
    else:
        # 日常闭环：先把到期未评价的预判补评，再用结果重估模型
        if _eval_pending(mkt):
            refit_models()

    asof_live = pd.Timestamp(bench["date"].iloc[-1]).normalize()
    target_live = pd.Timestamp(D.next_trading_day(asof_live.date()))
    pl = predict_payload(mkt, asof_live, target_live, source="live")

    # 深度层：以最近一个"已收盘"交易日为准，剔除当日盘中快照
    dp = depth_payload(mkt, asof_closed, pl)
    rp = rescue_payload(mkt, asof_closed)
    if "decision" in dp:
        pl["execution"] = dp["decision"]
        # 预判区间与尾部风险改用经验分布，比正态更可信
        tail = (dp.get("vol") or {}).get("tail")
        if tail:
            pl["tail"] = tail
    save_prediction(pl)

    with db() as c:
        last_eval = c.execute(
            "SELECT target,asof,direction,prob_up,exp_ret,exp_lo,exp_hi,actual_pct,hit,brier,"
            "payload,e_gap,e_intra,p_gap_up,actual_gap,actual_intra,gap_hit,source "
            "FROM pred WHERE evaluated=1 ORDER BY target DESC LIMIT 1").fetchone()
        errs = [dict(r) for r in c.execute(
            "SELECT day,target,kind,detail,diagnosis,action FROM errors "
            "ORDER BY id DESC LIMIT 14").fetchall()]
        hist = [dict(r) for r in c.execute(
            "SELECT target,direction,prob_up,actual_pct,hit,actual_gap,gap_hit,p_gap_up "
            "FROM pred WHERE evaluated=1 ORDER BY target DESC LIMIT 60").fetchall()]
        wrows = [dict(r) for r in c.execute(
            "SELECT name,weight,n FROM weights").fetchall()]
        whest = [dict(r) for r in c.execute(
            "SELECT day,name,weight FROM weight_hist ORDER BY day DESC LIMIT 400").fetchall()]

    last_eval_d = None
    if last_eval:
        last_eval_d = dict(last_eval)
        payload = json.loads(last_eval_d.pop("payload"))
        last_eval_d["signals"] = payload.get("signals", [])

    wh = pd.DataFrame(whest)
    weight_series = {}
    if not wh.empty:
        for name, g in wh.groupby("name"):
            g = g.sort_values("day")
            weight_series[name] = {
                "label": A.EXPERT_LABELS.get(name, name),
                "points": [[d, round(float(v), 4)] for d, v in zip(g["day"], g["weight"])]}

    # 60 日收盘序列：给主结论画一条走势，省得用户自己去想"现在在什么位置"
    bb = bench.tail(60)
    spark = {"date": [str(x)[:10] for x in pd.to_datetime(bb["date"])],
             "close": [round(float(x), 2) for x in bb["close"]]}

    state = {
        "spark": spark,
        "meta": {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "version": int(meta_get("version", 0) or 0),
                 "calib": pl["calib"],
                 "interval": interval_calib(),
                 "prob_calib": prob_calib(),
                 "intra_edge": meta_get("intra_edge", {}),
                 "param_log": param_log()[-12:],
                 "phase": mkt["phase"], "loaded_at": mkt["loaded_at"],
                 "load_seconds": mkt["elapsed"], "n_stock": int(len(mkt["spot"])),
                 "n_sector": int(len(mkt["sec_frames"])),
                 "calendar": D.calendar_coverage(),
                 "limit": mkt["limit"], "disclaimer": C.DISCLAIMER},
        "prediction": pl,
        "depth": dp,
        "rescue": rp,
        "stats": stats,
        "last_eval": last_eval_d,
        "history": hist[::-1],
        "errors": errs,
        "weights": sorted(wrows, key=lambda x: -abs(x["weight"])),
        "weight_series": weight_series,
        "overseas": mkt["leaders"].to_dict("records") if not mkt["leaders"].empty else [],
    }
    _assert_complete(state)
    return state


# 板块数量下限。正常 60 个左右；低于这个数说明板块列表或板块日线大面积失败，
# 此时的"板块为空"是故障，不是"今天没有强势板块"。
MIN_SECTORS_FOR_SNAPSHOT = 20


def snapshot_fresh_after_close() -> tuple[bool, str]:
    """收盘后是否已经算过这一次。返回 (是否可跳过重算, 该快照的生成时间)。

    为什么要这个判断
        收盘后行情不再变化，明天才用得上新数据。但服务每次启动都会 warm_start
        重算一次（50 秒），点刷新又算一次，自动刷新还会算。一晚上算十几次，
        每次结果完全一样——除了浪费时间，还在反复打行情接口，
        反而把自己打到限流。

    判据（只用本地已有信息，不为"要不要算"再去拉一次数据）
        · 已收盘：快照必须是**今天 15:00 之后**生成的。盘中算的那份不算数，
          因为它含有盘中价格，收盘后数字已经变了。
        · 休市：周末/节假日打开，只要快照是某个收盘后生成的、且不超过 5 天，
          就认为仍是最新的（周五收盘那份，周六周日都有效）。

    注意"盘中算的不算数"这一条：9:30 算出来的 prob_up 和 15:00 的收盘价不同，
    盘后必须重算一次才能真正定盘。这也是本函数只在 phase 已经是收盘/休市时
    才可能返回 True 的原因——盘中永远返回 False。
    """
    ph = D.market_phase()
    if ph not in ("已收盘", "休市"):
        return False, ""
    p = C.DATA_DIR / "state.json"
    if not p.exists():
        return False, ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  读不出来就当不新鲜，重算一次
        return False, ""
    made = str(((d.get("prediction") or {}).get("made_at")) or "")
    if len(made) < 16:
        return False, ""
    md, mt = made[:10], made[11:16]
    today = dt.date.today().strftime("%Y-%m-%d")

    if ph == "已收盘":
        # 必须是今天收盘之后生成的那一份
        if md == today and mt >= "15:00":
            return True, made
        return False, ""

    # 休市：不按"今天"卡，因为今天本来就没有新数据
    try:
        made_dt = dt.datetime.strptime(made, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, ""
    if mt >= "15:00" and (dt.datetime.now() - made_dt).days <= 5:
        return True, made
    return False, ""


def _assert_complete(state: dict) -> None:
    """快照不完整就抛异常，宁可不出新快照，也不要出一份残缺的。

    实测过一次：东财 clist 端点整段不可用（当天 15:50），板块 0 个、个股 0 只、
    ETF 0 只，但指数日线还能拉到，于是 prob_up 算出了 0.5249。
    这份数据被直接写进 state.json 覆盖掉上午的完整快照，
    页面上就是一个孤零零的"上涨概率 52.5%"加三块空白——
    读者完全分不出"今天没有机会"和"数据没拉到"，两者的决策含义相反。

    失败时抛错，由调用方保留上一次的快照并把错误显示到界面：
    **旧而完整，比新而残缺有用得多。**
    """
    pl = state.get("prediction") or {}
    n_sec = len(pl.get("sectors_all") or [])
    if n_sec < MIN_SECTORS_FOR_SNAPSHOT:
        raise RuntimeError(
            f"板块数据只拿到 {n_sec} 个（正常约 60），行情源可能限流或不可用。"
            "已保留上一次的完整快照，请稍后重试。")

__all__ = ["SCHEMA", "MIGRATIONS", "DEFAULT_GAP", "DEFAULT_INTRA", "db", "init_db", "meta_get", "meta_set", "load_learner", "save_learner", "load_weights", "gap_model", "intra_model", "save_prediction", "_phase", "_elapsed_fraction", "_is_live_bar", "_num", "build_etf_map", "record_etf_shares", "etf_share_changes", "load_market", "compute_breadth", "compute_capital", "news_sentiment", "sector_live_map", "bench_row_at", "panel_breadth_at", "sector_stat_at", "overseas_for", "ov_weighted", "build_context", "prob_calib", "apply_calib", "fit_prob_calib", "gap_scenario", "refit_models", "interval_calib", "fit_interval", "fit_intra_edge", "param_log", "_log_params", "predict_payload", "_stock_secid", "_rank_corr", "evaluate_target", "e_gap_text", "diagnose", "_DEPTH_CACHE", "depth_payload", "replay", "_eval_pending", "run_daily", "build_state"]
