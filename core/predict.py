"""预判层：把市场快照 + 模型参数合成一次完整的预判 payload。

这是唯一对外产出"明日观点"的地方；复盘读的是它落库的结果。

"""
from __future__ import annotations

from . import analytics as A
from . import chan as CH
from . import config as C
from . import datahub as D
import datetime as dt
import math
import numpy as np
import pandas as pd
from .store import (gap_model, intra_model, load_learner, stock_calib, stock_sample_count)
from .market import (_elapsed_fraction, _is_live_bar, _num, news_sentiment, sector_live_map)
from .model import (apply_calib, build_context, gap_scenario, interval_calib, ov_weighted, overseas_for)

def predict_payload(mkt: dict, asof: pd.Timestamp, target: pd.Timestamp,
                    source: str = "live") -> dict:
    live = source == "live"
    ov = None if live else overseas_for(mkt, asof, live=False)
    lp = mkt.get("leader_pct") if live else None
    ctx = build_context(mkt, asof, live=live, ov_ret=ov, leader_pct=lp)

    learner = load_learner()
    signals = [fn(ctx) for _, _, fn in A.EXPERTS]
    z, avail = learner.logit(signals)
    p_intra = float(np.clip(A.sigmoid(z), 0.03, 0.97))
    z_alpha = z - learner.b

    # ---- 跳空模型：由隔夜外盘线性解释 ----
    gm = gap_model()
    ov_pct = ov_weighted(ctx.overseas)
    if ctx.leader_pct is not None and np.isfinite(ctx.leader_pct):
        ov_pct = 0.84 * ov_pct + 0.16 * float(ctx.leader_pct)
    e_gap = float(gm["a"] + gm["b"] * ov_pct)
    s_gap = float(max(gm["s"], 0.18))

    # ---- 日内模型：微弱漂移 + 信号 ----
    im = intra_model()
    e_intra = float(im["mu"] + im["k"] * z_alpha)
    s_intra = float(max(im["s"], 0.30))

    e_total = e_gap + e_intra
    s_total = float(math.sqrt(s_gap ** 2 + s_intra ** 2))
    # 区间宽度用滚动残差分位数（conformal）自校准，概率用同一把尺子，避免"区间一套、概率一套"
    ic = interval_calib()
    half = float(ic["q"]) if ic.get("q") else 1.65 * s_total
    s_prob = half / 1.645 if ic.get("q") else s_total
    p_up_raw = float(np.clip(A.norm_cdf(e_total / s_prob), 0.02, 0.98))
    p_up = apply_calib(p_up_raw)
    # 跳空概率额外计入参数估计风险（残差波动 ×1.2），避免尾部过度自信
    p_gap_up = float(np.clip(A.norm_cdf(e_gap / (s_gap * 1.2)), 0.03, 0.97))
    lo, hi = e_total - half, e_total + half

    degraded = []
    if not ctx.overseas:
        degraded.append("隔夜外盘缺失，跳空按均值处理")
    if learner.n < 10:
        degraded.append("学习样本不足，权重接近先验")
    if ic.get("cov") is not None and ic["cov"] < 0.80:
        degraded.append(f"区间覆盖率偏低（{ic['cov']:.0%}），不确定性被低估")
    direction = A.direction_label(p_up)
    vol = abs(ctx.bench.get("vol20", 1.0)) or 1.0

    # ---- 板块 ----
    bench_row = ctx.bench
    ov_for_sector = ov_pct if not live else ov_weighted(ctx.overseas)

    def _bench_pct(key: str) -> float:
        v = bench_row.get(key, 0.0)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return v if np.isfinite(v) else 0.0

    cs = A.sector_cross_section(mkt["sec_frames"], asof, overseas_pct=ov_for_sector,
                                bench_r1=bench_row.get("r1", 0.0))
    net_map, live_map = {}, {}
    if live:
        live_map = sector_live_map(mkt["sector_spot"], mkt["sector_spot_c"])
        if not mkt["sector_spot"].empty:
            dfl = pd.concat([mkt["sector_spot"], mkt["sector_spot_c"]], ignore_index=True)
            net_map = dict(zip(dfl["code"], dfl["net_in"].fillna(0)))
    cs = A.score_sectors(cs, _bench_pct("r5"), market_exp=e_total,
                         net=net_map if live else None, live=live_map if live else None,
                         etf=mkt.get("etf_map") or None,
                         bench20=_bench_pct("r20"), bench120=_bench_pct("r120"))

    def _opt(v, nd: int = 2):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return round(v, nd) if np.isfinite(v) else None

    def _sec_row(r) -> dict:
        return {"code": r["code"], "name": r["name"], "score": float(r["score"]),
                "prob": round(float(r["prob"]), 3), "exp_ret": round(float(r["exp_ret"]), 2),
                "r1": round(float(r["r1"]), 2), "r5": round(float(r["r5"]), 2),
                "r20": round(float(r["r20"]), 2), "r60": _opt(r.get("r60")),
                "r120": _opt(r.get("r120")), "dd250": _opt(r.get("dd250")),
                "pos250": _opt(r.get("pos250"), 3), "low_age": _opt(r.get("low_age"), 0),
                "pot": bool(r.get("pot", False)), "pot_score": _opt(r.get("pot_score"), 1),
                "rel5": _opt(r.get("rel5")), "rel120": _opt(r.get("rel120")),
                "net_yi": round(float(net_map.get(r["code"], 0)) / 1e8, 2) if live else None,
                "etf": (mkt.get("etf_map") or {}).get(r["name"])}

    # 同产业链去重：电力设备/光伏/储能本质是同一批票。
    # 成员表按需拉取（每个板块一次请求），取够即停，不做全量预抓。
    _rows = {r["code"]: r for _, r in cs.iterrows()} if not cs.empty else {}
    mem_cache: dict[str, pd.DataFrame] = {}
    core_cache: dict[str, set] = {}

    def _members(code: str) -> pd.DataFrame:
        if code not in mem_cache:
            m = D.sector_members(code) if live else pd.DataFrame()
            mem_cache[code] = m
            core_cache[code] = (set(m.nlargest(30, "amount")["code"])
                                if not m.empty and "amount" in m else set())
        return mem_cache[code]

    def _overlap(a: str, b: str) -> float:
        ca, cb = core_cache.get(a, set()), core_cache.get(b, set())
        if not ca or not cb:
            return 0.0
        return len(ca & cb) / max(len(ca | cb), 1)

    def _diverse(frame, n: int, gap: float = 0.28, prefix_cap: int = 2) -> list[str]:
        """贪心去重：成分股重叠 + 名称同前缀。
        东财概念里"通信 / 通信设备 / 通信技术"成分股并不重合（Jaccard 0.02~0.25），
        去重逻辑不会拦，但一列里四个"通信"读起来就是没分类，所以名称前缀再限一道。"""
        sel: list[str] = []
        for _, r in frame.iterrows():
            code = r["code"]
            nm = str(r["name"])
            if sel and sum(1 for c in sel if str(_rows[c]["name"])[:2] == nm[:2]) >= prefix_cap:
                continue
            if live:
                _members(code)
                if any(_overlap(code, c) > gap for c in sel):
                    continue
            sel.append(code)
            if len(sel) >= n:
                break
        return sel

    if cs.empty:
        up_codes, down_codes, pot_codes = [], [], []
    else:
        up_codes = _diverse(cs.head(C.SECTOR_PICK_UP * 2), C.SECTOR_PICK_UP)
        down_codes = _diverse(cs.iloc[::-1].head(C.SECTOR_PICK_DOWN * 2), C.SECTOR_PICK_DOWN)
        pot_frame = (cs[cs["pot"]].sort_values("pot_score", ascending=False)
                     if "pot" in cs else cs.iloc[0:0])
        # 仍在走弱的不进潜伏：一边说"跌够了"、一边给它挂"最弱"标签，自相矛盾。
        # 但进了强势榜的不排除——"跌了半年 + 今天最强"恰恰是潜伏里最该抓的一类。
        pot_frame = pot_frame[~pot_frame["code"].isin(down_codes)]
        # 潜伏允许更大的同业容忍度：跌得久的往往是同一条产业链，全去重会被压成一条
        pot_codes = _diverse(pot_frame.head(max(C.SECTOR_POT * 3, 10)), C.SECTOR_POT, 0.40)

    sectors_up = [_sec_row(_rows[c]) for c in up_codes if c in _rows]
    sectors_down = [_sec_row(_rows[c]) for c in down_codes if c in _rows]
    sectors_potential = [_sec_row(_rows[c]) for c in pot_codes if c in _rows]
    # 阶段：已启动 / 转强 / 筑底。
    # 判据是"近 5 日相对大盘"，不是"有没有进强势榜"——后者只说明名次，
    # 一个跌了半年的板块 5 日跑赢大盘 7 个点、但绝对概率仍排第十，也是启动。
    _upset = set(up_codes)
    for _row in sectors_potential:
        _rel5 = _row.get("rel5") or 0.0
        _row["stage"] = ("已启动" if (_row["code"] in _upset or _rel5 >= 3.0)
                         else ("转强" if _rel5 >= 0.5 else "筑底"))
        _fr = mkt["sec_frames"].get(_row["code"])
        if _fr is not None and len(_fr) >= 20:
            _row["spark"] = [round(float(v), 1) for v in _fr["close"].tail(60)]
    # 按潜伏分排序（就是表里那一列），阶段只作标注：
    # 若改成"阶段优先"，分数列会变得无序，读的人无法验证排序依据。
    sectors_potential.sort(key=lambda r: -(r.get("pot_score") or 0))

    # ---- 个股：从去重后的看多板块中选 ----
    # 只取前 3 个板块 × 3 只 = 9 只。主页是"结论页"不是清单页：
    # 再多就会把上面的板块三段挤出首屏，完整信息在「板块」页。
    stocks, seen = [], set()
    s_calib = stock_calib()
    # 流动性门槛按"全日当量"折算：早盘成交额还没形成，直接比绝对额会误杀。
    # _elapsed_fraction 是已有的日内成交量累计曲线（集合竞价=0.02，收盘=1.0），
    # 板块的量比折算也是用它，口径保持一致。
    _frac = _elapsed_fraction() if live else 1.0
    _frac = max(float(_frac), 0.02)
    _floor = C.STOCK_MIN_AMOUNT * _frac
    for code in up_codes[:C.STOCK_SECTORS]:
        members = mem_cache.get(code)
        if members is None or members.empty:
            continue
        sec_row = _rows[code]
        ranked = A.score_stocks(members, float(sec_row["r1"]), float(sec_row["prob"]),
                                calib=s_calib, amount_floor=_floor)
        if ranked.empty:
            continue
        picked = 0
        for _, r in ranked.head(C.STOCK_PER_SECTOR * 3).iterrows():
            if picked >= C.STOCK_PER_SECTOR:
                break
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            picked += 1
            stocks.append({
                "code": r["code"], "name": r["name"], "sector": sec_row["name"],
                "price": round(float(r["price"]), 2), "prob": round(float(r["prob"]), 3),
                # prob_raw 是打分公式给的先验，prob 是校准后的值。
                # 两个都存：评价时只认 prob_raw（否则会拿"已经收缩过的值"再去拟合收缩系数，
                # 越迭代越平，最后所有个股概率都挤到 0.5 附近，分不出强弱）。
                "prob_raw": round(float(r["prob_raw"]), 3),
                "calibrated": bool(r["calibrated"]),
                "pct": round(float(r["pct"]), 2),
                "amount_yi": round(float(r["amount"]) / 1e8, 2),
                "reasons": A.stock_reasons(r, sec_row["name"], float(sec_row["r1"])),
            })

    # 名称炒作：今天市场是不是在按"名字"定价（东方系、洋字辈、龙字头）。
    # 只在实盘算——回放没有当日全市场快照，硬算会拿错数据。
    # 这是**描述**不是预测：名称炒作没有基本面锚，进去的只有情绪，
    # 所以它不进概率、不进个股打分，只在界面上告诉读者"今天涨的是哪一类"。
    name_themes = []
    if live and mkt.get("spot") is not None:
        try:
            name_themes = A.name_theme_scan(mkt["spot"])
        except Exception:  # noqa: BLE001  这个功能失败了不能拖垮主流程
            name_themes = []

    # ---- 缠论结构：只描述，不参与概率 ----
    # 回测显示买点后 3 日确有超额（n=14，留一法不翻转，置换检验 p=0.000），
    # 但 5.3 年只有 24 个信号——这个量级不足以估出一个可信的权重。
    # 硬塞进概率里只会让 0.58 这个数字看起来更权威，实际更不可靠。
    # 所以它的定位是"事件提示"：出现时明确告诉你，权重仍为 0。
    chan_info = {}
    try:
        _bench = mkt.get("bench_raw")
        if _bench is not None and not _bench.empty:
            _b = _bench.iloc[:-1] if _is_live_bar(_bench) else _bench
            chan_info = CH.summarize(_b.reset_index(drop=True))
    except Exception:  # noqa: BLE001  缠论算不出来不能拖垮主流程
        chan_info = {}

    # 成分股没拉到的板块必须说出来。少了这几只个股本身不致命，
    # 但"为什么只有 3 只"必须能从界面回答，不能让人以为系统就推荐 3 只。
    _miss = list(getattr(D, "FALLBACK_USED", {}).get("sector_members") or [])
    if _miss:
        degraded.append(f"{len(_miss)} 个板块的成分股未取到，个股名单不完整")
    if getattr(D, "FALLBACK_USED", {}).get("etf"):
        # ETF 整块空着比报错更误导：读者会以为"今天没有资金异动"，
        # 而不是"这条数据没拿到"。两者含义完全不同。
        degraded.append("ETF 行情未取到，资金流入/流出与板块 ETF 映射为空")
    if getattr(D, "FALLBACK_USED", {}).get("stock_spot") == "sina":
        # 换了源就要说清楚少了什么。新浪没有量比、主力净流入、所属行业，
        # 对应的因子会被 robust_z 压成 0（等于不参与），
        # 但读者需要知道"今天的个股排名少了两个维度"，而不是以为它们都是中性。
        degraded.append("全市场快照来自新浪备用源：缺量比、主力净流入与行业，"
                        "资金类因子按 0 处理")

    sectors_all = ([_sec_row(_rows[c]) for c in
                    sorted(_rows, key=lambda c: -float(_rows[c]["prob"]))] if _rows else [])

    etf_panel = {"inflow": [], "outflow": [], "n": 0}
    ef = mkt.get("etf")
    if ef is not None and not ef.empty:
        big = ef[ef["amount"] >= 3e7]
        etf_panel["n"] = int(len(big))

        def _e_row(r) -> dict:
            return {"code": str(r["code"]), "name": str(r["name"]),
                    "pct": round(_num(r.get("pct")), 2),
                    "net_yi": round(_num(r.get("net_in")) / 1e8, 2),
                    "amount_yi": round(_num(r.get("amount")) / 1e8, 2),
                    "net_ratio": round(_num(r.get("net_ratio")), 2)}
        etf_panel["inflow"] = [_e_row(r) for _, r in big.nlargest(6, "net_in").iterrows()]
        etf_panel["outflow"] = [_e_row(r) for _, r in big.nsmallest(4, "net_in").iterrows()]

    drivers = []
    for s in sorted(avail, key=lambda x: -abs(x.score)):
        if abs(s.score) < 0.05:
            continue
        drivers.append({"key": s.key, "label": s.label, "score": round(s.score, 3),
                        "text": s.detail, "dir": "偏多" if s.score > 0 else "偏空"})

    ov_txt = [f"{n}{v:+.2f}%" for n, v in (ctx.overseas or {}).items()]
    if live and mkt.get("leaders") is not None and not mkt["leaders"].empty:
        ld = mkt["leaders"]
        for _, r in ld.reindex(ld["pct"].abs().sort_values(ascending=False).index).head(3).iterrows():
            ov_txt.append(f"{r['name']}{r['pct']:+.1f}%")

    sent_score, news_top = news_sentiment(mkt["news"]) if live else (None, [])

    return {
        "asof": pd.Timestamp(asof).strftime("%Y-%m-%d"),
        "target": pd.Timestamp(target).strftime("%Y-%m-%d"),
        "made_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "phase": mkt["phase"] if live else "回放",
        "direction": direction,
        "prob_up": round(p_up, 4),
        "prob_up_raw": round(p_up_raw, 4),
        "z": round(z, 4),
        "exp_ret": round(e_total, 2),
        "exp_lo": round(lo, 2),
        "exp_hi": round(hi, 2),
        "vol": round(vol, 3),
        "e_gap": round(e_gap, 3),
        "s_gap": round(s_gap, 3),
        "half_width": round(half, 3),
        "s_prob": round(s_prob, 3),
        "degraded": degraded,
        "p_gap_up": round(p_gap_up, 4),
        "e_intra": round(e_intra, 3),
        "s_intra": round(s_intra, 3),
        "p_intra_up": round(p_intra, 4),
        "ov_pct": round(ov_pct, 3),
        "calib": {"gap": gm, "intra": im, "b": round(learner.b, 4), "n_update": learner.n},
        "stock_calib": {"base": round(float(s_calib.get("base", 0.5)), 4),
                        "lam": round(float(s_calib.get("lam", 0.35)), 3),
                        "slope": round(float(s_calib.get("slope", 1.0)), 3),
                        "n": int(s_calib.get("n", 0) or 0),
                        # n_seen 是"已经拿到真实涨跌、可用于拟合的行数"，
                        # 它先于 n 增长。只看 n 的话，界面会长期显示 0/40，
                        # 读者无法区分"在积累"和"压根没在积累"。
                        "n_seen": stock_sample_count(),
                        "ready": int(s_calib.get("n", 0) or 0) >= A.STOCK_CALIB_MIN_N,
                        "min_n": A.STOCK_CALIB_MIN_N,
                        "hit_rate": s_calib.get("hit_rate"),
                        "brier": s_calib.get("brier"),
                        "brier_raw": s_calib.get("brier_raw"),
                        "improved": s_calib.get("improved"),
                        # weak：校准后发现个股概率几乎分不开，等于承认
                        # "这个打分模型目前对次日涨跌没有稳定预测力"。
                        "weak": bool(s_calib.get("weak", False))},
        "bench_close": round(float(ctx.bench.get("close", 0)), 2),
        "bench_chg": round(float(ctx.bench.get("r1", 0)), 2),
        "signals": [{"key": s.key, "label": s.label, "score": round(s.score, 3),
                     "detail": s.detail, "weight": round(learner.w.get(s.key, 0), 3),
                     "available": s.available, "live": s.live_only} for s in signals],
        "sectors_up": sectors_up,
        "sectors_down": sectors_down,
        "sectors_potential": sectors_potential,
        "sectors_all": sectors_all,
        "etf_panel": etf_panel,
        "stocks": stocks,
        "name_themes": name_themes,
        "chan": chan_info,
        "drivers": drivers[:6],
        "overseas": ov_txt,
        "internals": ctx.internals,
        "breadth": ctx.panel_breadth,
        "sector_stat": ctx.sector_stat,
        "capital": ctx.capital,
        "news_score": None if sent_score is None else round(sent_score, 3),
        "news": news_top,
        "scenario": gap_scenario(e_gap) if live else None,
        "sector_ic_frame": [{"code": r["code"], "name": r["name"], "z": float(r["z"]),
                             "prob": float(r["prob"])}
                            for _, r in cs.iterrows()] if not cs.empty else [],
    }

