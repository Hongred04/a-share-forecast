"""前后端契约测试：校验 data/state.json 的字段与取值。

为什么需要它
    前端 `web/app.js` 直接读 `state` 的字段名，两边没有类型系统约束。
    过去几次改动的故障都出在这里：后端改了字段名、前端还在读旧名，
    页面只是安静地显示 "—"，没有任何报错。
    这个脚本把"契约"变成可执行断言，改 payload 时先在这里失败。

覆盖三类问题
    1. 结构性：必需字段有没有、类型对不对
    2. 数值性：概率在 [0,1]、区间包含中值、VaR 为负、Sharpe 量级合理
    3. 一致性：多处重复的字段是否互相一致（板块数、权重和、可执行性与方向）

用法
    python tests/contract.py            # 校验 data/state.json
    python run.py test                  # 连同渲染冒烟测试一起跑
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)


def need(obj, key: str, where: str, typ=None):
    if not isinstance(obj, dict) or key not in obj:
        fails.append(f"{where}.{key} 缺失")
        return None
    val = obj[key]
    if typ is not None and not isinstance(val, typ):
        fails.append(f"{where}.{key} 类型应为 {typ.__name__}，实际 {type(val).__name__}")
    return val


def rng(val, lo: float, hi: float, where: str, allow_none: bool = False) -> None:
    if val is None:
        if not allow_none:
            fails.append(f"{where} 不应为空")
        return
    if not isinstance(val, (int, float)) or not math.isfinite(val):
        fails.append(f"{where} 非有限数值：{val!r}")
    elif not (lo <= val <= hi):
        fails.append(f"{where} = {val} 超出 [{lo}, {hi}]")


def main() -> int:
    if not STATE.exists():
        print(f"✗ 找不到 {STATE}，先运行 python run.py build")
        return 2
    S = json.loads(STATE.read_text(encoding="utf-8"))

    # ---------------- 顶层 ----------------
    for k in ("meta", "prediction", "depth", "stats", "errors", "weights",
              "weight_series", "history", "overseas"):
        need(S, k, "state")

    meta = S.get("meta") or {}
    for k in ("generated_at", "version", "phase", "n_sector", "disclaimer"):
        need(meta, k, "meta")
    need(meta, "calib", "meta", dict)
    need(meta, "interval", "meta", dict)
    need(meta, "intra_edge", "meta", dict)
    # 日历覆盖度：前端据此提示"交易日历已过期"。
    # 少了这个字段，提示条会静默消失——而它恰恰是防止 target 算错的那道闸。
    need(meta, "calendar", "meta", dict)
    cal = meta.get("calendar") or {}
    for k in ("covered", "stale", "msg"):
        need(cal, k, "meta.calendar")
    check(isinstance(cal.get("stale"), bool), "meta.calendar.stale 应为布尔")
    if cal.get("stale"):
        check(bool(cal.get("msg")), "meta.calendar.stale 为真时必须带 msg（界面要显示这句话）")

    # ---------------- 预判 ----------------
    pl = S.get("prediction") or {}
    for k in ("target", "asof", "direction", "prob_up", "exp_ret", "exp_lo", "exp_hi",
              "e_gap", "e_intra", "p_gap_up", "p_intra_up", "vol",
              "signals", "sectors_up", "sectors_down", "sectors_potential", "sectors_all",
              "etf_panel", "stocks", "execution", "tail"):
        need(pl, k, "prediction")
    check(pl.get("direction") in ("偏多", "偏空", "震荡"),
          f"prediction.direction 取值异常：{pl.get('direction')!r}")
    rng(pl.get("prob_up"), 0.02, 0.98, "prediction.prob_up")
    rng(pl.get("p_gap_up"), 0.0, 1.0, "prediction.p_gap_up")
    rng(pl.get("p_intra_up"), 0.0, 1.0, "prediction.p_intra_up")
    lo, mid, hi = pl.get("exp_lo"), pl.get("exp_ret"), pl.get("exp_hi")
    if None not in (lo, mid, hi):
        check(lo <= mid <= hi, f"prediction 区间不包含中值：{lo} / {mid} / {hi}")
        check(hi - lo < 20, f"prediction 区间过宽：{hi - lo:.2f}pct")

    # 三段之和 = 全天
    if None not in (pl.get("e_gap"), pl.get("e_intra"), pl.get("exp_ret")):
        gap_sum = pl["e_gap"] + pl["e_intra"]
        check(abs(gap_sum - pl["exp_ret"]) < 0.05,
              f"e_gap + e_intra = {gap_sum:.3f} 与 exp_ret {pl['exp_ret']:.3f} 不一致")

    # 可执行性与决策自洽
    ex = pl.get("execution") or {}
    for k in ("actionable", "headline", "why", "risk_cap", "risk_level", "execution_advice"):
        need(ex, k, "prediction.execution")
    check(isinstance(ex.get("actionable"), bool), "execution.actionable 应为布尔")
    check(ex.get("actionable") == (ex.get("trade") is not None),
          "execution.actionable 与 trade 是否为空不一致")
    rng(ex.get("risk_cap"), 0.1, 1.0, "execution.risk_cap")

    tail = pl.get("tail") or {}
    for k in ("var95_bp", "var99_bp", "cvar95_bp"):
        need(tail, k, "prediction.tail")
    if None not in (tail.get("var95_bp"), tail.get("var99_bp"), tail.get("cvar95_bp")):
        check(tail["var95_bp"] < 0, "tail.var95_bp 应为负")
        check(tail["var99_bp"] <= tail["var95_bp"], "tail.var99_bp 应比 var95 更负")
        check(tail["cvar95_bp"] <= tail["var95_bp"], "tail.cvar95_bp 应比 var95 更负")

    # 板块 / 个股
    n_sec = meta.get("n_sector")
    if isinstance(n_sec, int) and n_sec > 0:
        check(len(pl.get("sectors_all") or []) == n_sec,
              f"sectors_all 应有 {n_sec} 条，实际 {len(pl.get('sectors_all') or [])}")
    for s in (pl.get("sectors_all") or [])[:5]:
        for k in ("code", "name", "prob", "exp_ret", "r1"):
            need(s, k, "sectors_all[]")
        rng(s.get("prob"), 0.0, 1.0, f"sectors_all[{s.get('name')}].prob")

    # 潜伏名单：只允许"跌久 + 低位"的板块进入，且必须自带判据字段，
    # 否则前端会退化成一张没有依据的排行榜。
    for s in (pl.get("sectors_potential") or []):
        for k in ("dd250", "pos250", "r5", "rel120", "pot_score", "stage"):
            need(s, k, "sectors_potential[]")
        check(s.get("pot") is True, f"sectors_potential[{s.get('name')}] 未标记 pot")
        if s.get("dd250") is not None:
            check(s["dd250"] <= -15, f"sectors_potential[{s.get('name')}] 回撤不够深：{s['dd250']}")
        check(s.get("stage") in ("已启动", "转强", "筑底"),
              f"sectors_potential[{s.get('name')}] 阶段非法：{s.get('stage')!r}")
        rng(s.get("pot_score"), 0.0, 100.0, f"sectors_potential[{s.get('name')}].pot_score")
    # 已启动必须真的有"启动"证据：进强势榜，或近 5 日跑赢大盘 3 个点
    up_codes = {x.get("code") for x in (pl.get("sectors_up") or [])}
    for s in (pl.get("sectors_potential") or []):
        if s.get("stage") == "已启动":
            ok = s.get("code") in up_codes or (s.get("rel5") or 0) >= 2.9
            check(ok, f"{s.get('name')} 标了已启动但既不在强势榜、rel5 也不够：{s.get('rel5')}")
    # 潜伏池是横截面前 8，占比过高说明阈值退化成"全部"
    n_pot = sum(1 for x in (pl.get("sectors_all") or []) if x.get("pot"))
    if isinstance(n_sec, int) and n_sec > 0:
        check(n_pot <= max(3, int(n_sec * 0.3)),
              f"潜伏池 {n_pot}/{n_sec} 过大，筛选口径失效")
    codes = [x.get("code") for x in (pl.get("stocks") or [])]
    check(len(codes) == len(set(codes)), "stocks 存在重复标的")
    for s in (pl.get("stocks") or [])[:5]:
        for k in ("code", "name", "sector", "prob", "reasons", "prob_raw", "calibrated"):
            need(s, k, "stocks[]")
        rng(s.get("prob"), 0.0, 1.0, f"stocks[{s.get('name')}].prob")
        rng(s.get("prob_raw"), 0.0, 1.0, f"stocks[{s.get('name')}].prob_raw")
        check(isinstance(s.get("calibrated"), bool),
              f"stocks[{s.get('name')}].calibrated 应为布尔（界面靠它决定说不说未校准）")
        # 校准只会把概率往基准率收缩，不会放大到区间外。若 prob 与 prob_raw
        # 差得离谱，说明校准层写错了，必须当场发现而不是等界面看起来怪。
        d = abs(float(s["prob"]) - float(s["prob_raw"]))
        check(d <= 0.20, f"stocks[{s.get('name')}] 校准前后差 {d:.3f}，超出合理收缩幅度")

    # ---------------- 个股概率校准状态 ----------------
    sc = pl.get("stock_calib")
    need(pl, "stock_calib", "prediction", dict)
    if isinstance(sc, dict):
        for k in ("base", "lam", "n", "n_seen", "ready", "min_n"):
            need(sc, k, "prediction.stock_calib")
        check(isinstance(sc.get("ready"), bool), "stock_calib.ready 应为布尔")
        check(isinstance(sc.get("min_n"), int) and sc["min_n"] > 0,
              "stock_calib.min_n 应为正整数")
        rng(sc.get("base"), 0.0, 1.0, "stock_calib.base")
        # ready 必须严格等于 n >= min_n，两处口径不能各算各的
        check(bool(sc.get("ready")) == (int(sc.get("n") or 0) >= int(sc.get("min_n") or 40)),
              f"stock_calib.ready 与 n/min_n 不一致：n={sc.get('n')} "
              f"min_n={sc.get('min_n')} ready={sc.get('ready')}")
        # 未就绪时，任何一只个股都不该标 calibrated=True
        if not sc.get("ready"):
            wrong = [s.get("name") for s in (pl.get("stocks") or [])
                     if s.get("calibrated")]
            check(not wrong, f"校准未就绪，但这些个股标了已校准：{wrong}")
        # 就绪时必须带可核对的成绩，否则"已校准"三个字没有依据
        if sc.get("ready"):
            for k in ("hit_rate", "brier", "brier_raw"):
                check(sc.get(k) is not None, f"stock_calib 就绪但缺 {k}")

    # ---------------- 名称炒作（只描述，不参与预测） ----------------
    nt = pl.get("name_themes")
    check(isinstance(nt, (list, type(None))), "name_themes 应为数组或 null")
    if isinstance(nt, list):
        check(len(nt) <= 4, f"name_themes 最多 4 条，实际 {len(nt)}（多了是噪音）")
        for x in nt:
            for k in ("label", "n", "lift", "names"):
                need(x, k, "name_themes[]")
            check(isinstance(x.get("n"), int) and x["n"] >= 3,
                  f"name_themes[{x.get('label')}] 命中数应 >= 3")
            check(float(x.get("lift", 0)) >= 3.0,
                  f"name_themes[{x.get('label')}] lift 应 >= 3（否则是巧合不是主题）")
            check(isinstance(x.get("names"), list) and len(x["names"]) >= 3,
                  f"name_themes[{x.get('label')}] 必须列出命中的股票名，"
                  f"否则读者无法自己判断这算不算主题")
            # 主题字必须真的出现在所列股票名里，不能张冠李戴
            miss = [nm for nm in x["names"] if x["label"] not in str(nm)]
            check(not miss, f"name_themes[{x['label']}] 列了不含该字的股票：{miss}")
        # 这一层是描述性的，绝不能反过来影响个股概率
        check(pl.get("prob_up") is not None, "name_themes 存在时主结论仍应正常输出")

    # ---------------- 缠论结构（只描述，不参与概率） ----------------
    # 可选字段：算不出来时可以是空对象，这时前端整节不显示
    ck = pl.get("chan")
    if ck:
        for k in ("bis", "zhongshus", "last_bi", "signals_total"):
            need(ck, k, "chan")
        lb = ck.get("last_bi") or {}
        for k in ("dir", "high", "low", "confirmed"):
            need(lb, k, "chan.last_bi")
        check(lb.get("dir") in ("up", "down"), f"chan.last_bi.dir 必须是 up/down，实际 {lb.get('dir')}")
        check(float(lb.get("high", 0)) >= float(lb.get("low", 1e12)),
              "chan.last_bi 的高低点必须有序")
        # 样本量是这套结论的可信度基础，必须随结论一起出去
        for side in ("buy", "sell"):
            st = ck.get(side) or {}
            if st.get("n"):
                need(st, "mean", f"chan.{side}")
                check(-30 < float(st["mean"]) < 30,
                      f"chan.{side}.mean 量级异常：{st.get('mean')}（应为百分数）")
                check(0 <= float(st.get("win", -1)) <= 1,
                      f"chan.{side}.win 必须在 0~1：{st.get('win')}")
        recent = ck.get("recent")
        if recent:
            check(recent.get("kind") in
                  ("buy1", "buy2", "buy3", "sell1", "sell2", "sell3", "pzbuy", "pzsell"),
                  f"chan.recent.kind 不在已知买卖点内：{recent.get('kind')}")
            check(recent.get("age", 99) <= 10, "chan.recent 只应保留 10 个交易日内的信号")

    etf = pl.get("etf_panel") or {}
    for k in ("inflow", "outflow", "n"):
        need(etf, k, "prediction.etf_panel")

    # ---------------- 深度层 ----------------
    dp = S.get("depth") or {}
    check("error" not in dp, f"depth 计算失败：{dp.get('error')}")
    for k in ("segments", "vol", "regime", "intraday", "xsection", "selection_bias", "cost_bp"):
        need(dp, k, "depth")

    seg = (dp.get("segments") or {}).get("segments") or []
    check(len(seg) == 3, f"depth.segments 应有 3 段，实际 {len(seg)}")
    keys = {s.get("key") for s in seg}
    check(keys == {"overnight", "intraday", "full"},
          f"depth.segments 的 key 异常：{keys}")
    for s in seg:
        for k in ("label", "mean_bp", "recent_bp", "win", "sharpe", "window"):
            need(s, k, "depth.segments[]")
        rng(s.get("win"), 0.0, 1.0, f"depth.segments[{s.get('key')}].win")
        rng(s.get("sharpe"), -6.0, 6.0, f"depth.segments[{s.get('key')}].sharpe")

    vol = dp.get("vol") or {}
    for k in ("forecast_vol_pct", "annual_vol_pct", "percentile", "regime", "qlike", "tail"):
        need(vol, k, "depth.vol")
    rng(vol.get("forecast_vol_pct"), 0.05, 15.0, "depth.vol.forecast_vol_pct")
    rng(vol.get("percentile"), 0.0, 1.0, "depth.vol.percentile")
    check(vol.get("regime") in ("低波", "中波", "高波"),
          f"depth.vol.regime 取值异常：{vol.get('regime')!r}")
    q = vol.get("qlike") or []
    check(len(q) >= 3, "depth.vol.qlike 至少应有 3 个模型")
    if len(q) >= 2:
        vals = [x.get("qlike") for x in q if x.get("qlike") is not None]
        check(vals == sorted(vals), "depth.vol.qlike 未按 QLIKE 升序（第一行应为最优）")

    reg = dp.get("regime") or {}
    for k in ("base_rate", "rows", "current"):
        need(reg, k, "depth.regime")
    rng(reg.get("base_rate"), 0.3, 0.7, "depth.regime.base_rate")
    check(len(reg.get("rows") or []) == 3, "depth.regime.rows 应有 3 档")
    for r in reg.get("rows") or []:
        rng(r.get("prior"), 0.3, 0.7, f"depth.regime[{r.get('label')}].prior", True)

    ci = dp.get("intraday") or {}
    for k in ("base_bp", "rows", "current"):
        need(ci, k, "depth.intraday")
    rows_i = ci.get("rows") or []
    check(len(rows_i) == 5, f"depth.intraday.rows 应有 5 桶，实际 {len(rows_i)}")
    for r in rows_i:
        for k in ("label", "n", "median_bp", "mean_bp", "se_bp", "net_bp",
                  "significant", "reliable"):
            need(r, k, "depth.intraday.rows[]")
        rng(r.get("mean_bp"), -300, 300, f"intraday[{r.get('label')}].mean_bp", True)
        check(isinstance(r.get("reliable"), bool), "intraday.reliable 应为布尔")
        # 可靠性定义必须与样本量一致，否则界面上的"样本不足"标签会骗人
        if isinstance(r.get("n"), int) and isinstance(r.get("reliable"), bool):
            check(r["reliable"] == (r["n"] >= 60),
                  f"intraday[{r.get('label')}].reliable 与 n={r['n']} 不一致")

    xs = dp.get("xsection") or {}
    need(xs, "rows", "depth.xsection")
    need(xs, "pit_warning", "depth.xsection")
    for r in xs.get("rows") or []:
        for k in ("factor", "ic", "t", "spread_bp", "net_bp", "survives"):
            need(r, k, "depth.xsection.rows[]")
        rng(r.get("ic"), -0.3, 0.3, f"xsection[{r.get('factor')}].ic")
        rng(r.get("t"), -12, 12, f"xsection[{r.get('factor')}].t")
        # Bonferroni 门槛写死在 depth 里，这里对齐校验
        if isinstance(r.get("t"), (int, float)):
            check(r["survives"] == (abs(r["t"]) > 2.58),
                  f"xsection[{r.get('factor')}].survives 与 t={r['t']} 不一致")

    sb = dp.get("selection_bias") or {}
    for k in ("n_trials", "years", "threshold"):
        need(sb, k, "depth.selection_bias")
    rng(sb.get("threshold"), 0.1, 3.0, "depth.selection_bias.threshold")

    # ---------------- 其它区块 ----------------
    st = S.get("stats") or {}
    for k in ("hit_rate", "base_rate", "gap_hit_rate", "brier", "n"):
        need(st, k, "stats")
    rng(st.get("hit_rate"), 0.0, 1.0, "stats.hit_rate")
    rng(st.get("gap_hit_rate"), 0.0, 1.0, "stats.gap_hit_rate")
    rng(st.get("brier"), 0.0, 0.5, "stats.brier")

    # 权重是 logistic 系数（带符号，逐个裁剪在 ±1.8），不是归一化的加权平均，
    # 因此"和为 1"不成立——这里只约束取值范围与信号个数。
    w = S.get("weights") or []
    check(len(w) == 10, f"weights 应有 10 个信号，实际 {len(w)}")
    for x in w:
        rng(x.get("weight"), -1.8, 1.8, f"weights[{x.get('name')}]")
    check(any(abs(x.get("weight", 0)) > 0.15 for x in w), "所有权重都接近 0，模型可能未训练")
    check(any(x.get("weight", 0) < 0 for x in w), "没有负权重：带符号学习可能失效")
    check(len(S.get("weight_series") or {}) >= 3, "weight_series 记录过少")

    hist = S.get("history") or []
    if len(hist) >= 2:
        dates = [x.get("target") for x in hist]
        check(dates == sorted(dates), "history 未按日期升序")

    # 前端实际读取的字段名白名单：多一个少一个都会在页面上静默失败
    # ---------------- 走势序列 ----------------
    spark = S.get("spark") or {}
    check(len(spark.get("close") or []) >= 20, "spark.close 至少应有 20 个点")
    check(len(spark.get("date") or []) == len(spark.get("close") or []),
          "spark 的日期与收盘长度不一致")

    # ---------------- 护盘共振层 ----------------
    rs = S.get("rescue") or {}
    check(not rs.get("error"), f"rescue 计算失败: {rs.get('error')}")
    lights = rs.get("lights") or []
    check(len(lights) == 5, f"护盘灯应为 5 盏，实际 {len(lights)}")
    for x in lights:
        check(x.get("state") in ("green", "red", "grey"),
              f"灯状态非法: {x.get('name')}={x.get('state')}")
        check(bool(x.get("name")) and "note" in x, f"灯字段不全: {x}")
    g = sum(1 for x in lights if x.get("state") == "green")
    r = sum(1 for x in lights if x.get("state") == "red")
    check(rs.get("green") == g and rs.get("red") == r, "灯的计数与逐项状态不一致")
    want = "资金面偏暖" if g >= 3 else ("资金面偏冷" if r >= 3 else "中性")
    check(rs.get("verdict") == want, f"判定与计数不一致: {rs.get('verdict')} vs {want}")

    t30 = rs.get("tail30")
    if t30:
        for k in ("date", "pct", "mean", "n", "win"):
            check(k in t30, f"tail30 缺字段 {k}")
        rng(t30.get("pct"), -5, 5, "tail30.pct")

    rc = rs.get("rescue") or {}
    conds = rc.get("conds") or []
    check(len(conds) == 3, f"救市三条应为 3 项，实际 {len(conds)}")
    check(bool(rc.get("triggered")) == all(c.get("on") for c in conds),
          "triggered 与三条条件不一致")

    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for field in ("prob_up", "exp_lo", "execution", "forecast_vol_pct",
                  "median_bp", "survives", "selection_bias", "risk_cap",
                  "rescue", "lights", "triggered"):
        check(field in js, f"app.js 未引用契约字段 {field}（后端改了名字？）")

    # ---------------- 汇总 ----------------
    if fails:
        print(f"✗ 契约检查失败 {len(fails)} 项：")
        for f in fails:
            print("   -", f)
        return 1
    print("✓ 契约检查通过（state.json 字段、取值与前后端一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
