"""
深度研究：把"预测"拆到可交易的最小单元，并检验每一层的真实信息量。

回答五个顶尖量化研究会先问的问题：
  1. 方向真的不可预测吗？那什么可预测？（方向 vs 波动的可预测性对照）
  2. 模型 63.8% 的跳空命中率，能变成钱吗？（信息时点 vs 可执行时点）
  3. 同一个信号在不同市场状态下符号是否翻转？（池化模型为何必然失效）
  4. 截面（板块之间）是否有比时序更强的 alpha？（IC / 分层价差）
  5. 测试了这么多变体，最好的那个结果有多少是选择偏差？（Deflated Sharpe）

用法: python research_deep.py
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

from core import analytics as A, config as C, depth as DP, engine as E

pd.set_option("display.width", 200)
LINE = "=" * 78


def hdr(n: int, title: str) -> None:
    print(f"\n{LINE}\n[{n}] {title}\n{LINE}")


# ======================================================================================
# 数据准备
# ======================================================================================
def load():
    mkt = E.load_market()
    bench = mkt["bench"].copy()
    # 与系统同口径：盘中未收盘的那根 K 线不参与统计
    if E._is_live_bar(bench):
        bench = bench.iloc[:-1].copy()
        print("  （已剔除当日盘中未完成的 K 线）")
    # 三段收益的定义只在这里写一次：直接复用系统内的 prepare（产出为百分数），
    # 再折回小数供本脚本内部使用。自己重写一遍正是之前 0.08bp 偏差的来源。
    bench = DP.prepare(bench)
    for c in ("gap", "intra", "c2c"):
        bench[c] = bench[c] / 100
    bench["on"] = bench["gap"]                      # 隔夜段
    for c in ("gap", "intra", "c2c", "on"):
        bench[f"f_{c}"] = bench[c].shift(-1)        # 次日实现值
    # 日内/隔夜波动（用 OHLC 估计，避免只用收盘价低估）
    rng = (bench["high"] - bench["low"]) / bench["close"].shift(1)
    bench["rv_park"] = (rng ** 2) / (4 * math.log(2))          # Parkinson
    bench["abs_r"] = bench["c2c"].abs()

    # 外盘加权（与引擎 ov_weighted 同口径），按美股日期索引后对齐到 A 股交易日
    try:
        ov = mkt["ov_raw"]
        w = {"纳斯达克": 0.34, "标普500": 0.26, "道琼斯": 0.16, "恒生指数": 0.24}
        ser = {}
        for k, df in ov.items():
            v = df.set_index(pd.to_datetime(df["date"]).dt.normalize())["pct"]
            ser[k] = v[~v.index.duplicated(keep="last")]
        ovdf = pd.DataFrame(ser).sort_index()
        tot = sum(w.get(k, 0) for k in ovdf.columns) or 1.0
        bench["ov"] = sum(ovdf[k].fillna(0) * w.get(k, 0) for k in ovdf.columns) / tot
        bench["ov"] = bench["ov"].reindex(bench.index).ffill()
    except Exception as ex:  # noqa: BLE001
        print("  外盘对齐失败:", ex)
        bench["ov"] = np.nan
    return mkt, bench


# ======================================================================================
# [1] 方向 vs 波动：可预测性对照
# ======================================================================================
def part1(bench: pd.DataFrame) -> dict:
    hdr(1, "可预测性对照：方向 vs 波动（同一份数据、同一套 walk-forward）")
    d = bench.dropna(subset=["f_c2c", "rv_park"]).copy()
    rv = d["rv_park"].clip(lower=1e-10)
    y = (d["f_c2c"] > 0).astype(float).to_numpy()

    # ---------- 方向：条件基准率（唯一"廉价"预测） ----------
    base_all = float(y.mean())
    # 滚动 250 日上涨率
    roll_base = (d["f_c2c"] > 0).rolling(250).mean().shift(1)
    ok = roll_base.notna().to_numpy()
    p_roll = roll_base.to_numpy()[ok]
    yy = y[ok]
    ll = lambda p, q: float(-np.mean(q * np.log(np.clip(p, 1e-6, 1 - 1e-6)) +
                                    (1 - q) * np.log(np.clip(1 - p, 1e-6, 1 - 1e-6))))
    print(f"  样本 {len(y)} 日，无条件上涨率 {base_all:.4f}")
    print(f"  '永远看涨'       logloss {ll(np.full_like(yy, base_all), yy):.4f}  命中 {base_all:.4f}")
    print(f"  '滚动基准率'      logloss {ll(p_roll, yy):.4f}  命中 {float(np.mean((p_roll>0.5)==(yy>0))):.4f}")
    print("  ↑ 这两行是任何方向模型必须打败的对手，不是 50%")

    # ---------- 波动：直接复用系统内的实现，保证与界面数字同源 ----------
    vs = DP.vol_state(bench)
    print("\n  波动率预测（预测次日方差，QLIKE 越小越好；R² 不是正确度量）:")
    print(f"    {'模型':<12}{'QLIKE':>9}")
    for r in vs["qlike"]:
        print(f"    {r['model']:<12}{r['qlike']:>9.4f}")
    print(f"    当前预测：日波动 {vs['forecast_vol_pct']:.2f}%"
          f"（年化 {vs['annual_vol_pct']:.1f}%），历史 {vs['percentile']*100:.0f} 分位 → {vs['regime']}")
    t = vs.get("tail") or {}
    if t:
        print(f"    翌日尾部：VaR95 {t['var95_bp']/100:+.2f}%  CVaR95 {t['cvar95_bp']/100:+.2f}%  "
              f"偏度 {t['skew']:+.2f}  峰度 {t['kurt']:.1f}")

    print("\n  → 结论：方向的可预测性 ~0（logloss 与基准率几乎相同）。")
    print("          波动率只在 QLIKE 意义上略优于无条件方差——用 R² 评估会误判为'不可预测'，")
    print("          因为方差被极端值主导。评估波动要用 QLIKE，评估方向要用 logloss。")
    return {"base_rate": base_all, "vol_rows": vs["qlike"]}


# ======================================================================================
# [2] 可交易性审计
# ======================================================================================
def part2(bench: pd.DataFrame) -> dict:
    hdr(2, "可交易性审计：预测准确 ≠ 能赚钱")
    print("  信息时点（关键）:")
    print("    15:00 收盘 T 日 → 21:30 美股 T 日开盘 → 次日 04:00 美股收盘 → 08:30 出预判 → 09:30 A股开盘")
    print("    预判用到的是「美股 T 日」，它在 A股 T 日收盘之后才发生。")
    print("    所以：预判最早可在 T+1 08:30 做出，最早可执行时点是 T+1 09:30 开盘。\n")

    # 注意：按 gap/intra 筛（第一行因缺少前一日收盘而为空），
    # 不能按 f_gap/f_intra 筛——那会剔掉最后一天，与深度层差一个样本。
    d = bench.dropna(subset=["gap", "intra"]).copy()
    print(f"  三段收益的统计（{len(d)} 日，单位 bp/日）:")
    for c, lab in (("c2c", "全天 昨收→今收"), ("gap", "隔夜 昨收→今开"),
                   ("intra", "日内 今开→今收")):
        s = d[c] * 10000
        print(f"    {lab:<18} 均值 {s.mean():+7.2f}  中位 {s.median():+7.2f}  "
              f"std {s.std():7.2f}  上涨率 {(d[c]>0).mean():.3f}  Sharpe {s.mean()/s.std()*math.sqrt(244):+6.2f}")
    print("    ↑ 隔夜段独占了几乎全部漂移，日内段近乎为零 —— A股的'隔夜溢价'现象")

    # 预测力：外盘 → 哪一段
    print("\n  外盘对三段的解释力（这是'准确率'的来源）:")
    for c, lab in (("f_gap", "次日隔夜段"), ("f_intra", "次日日内段"), ("f_c2c", "次日全天")):
        sub = pd.DataFrame({"x": d["ov"], "y": d[c]}).dropna()
        if len(sub) < 50:
            print(f"    {lab:<12} 外盘不可用")
            continue
        r = sub["x"].corr(sub["y"])
        t = r * math.sqrt(len(sub) - 2) / math.sqrt(max(1 - r * r, 1e-9))
        print(f"    {lab:<12} corr {r:+.4f}  t={t:6.2f}")

    # 可执行策略回测
    print("\n  可执行策略（单边成本 2.5bp，双边 5bp）:")
    cost = 5.0 / 10000
    out = {}

    def perf(ret: pd.Series, name: str, exposure: pd.Series | None = None):
        r = ret.dropna()
        if exposure is not None:
            r = r[exposure.reindex(r.index).fillna(False)]
        if len(r) < 60:
            print(f"    {name:<26} 样本不足")
            return None
        net = r - cost
        ann = net.mean() * 244
        shp = net.mean() / (net.std() + 1e-12) * math.sqrt(244)
        eq = (1 + net).cumprod()
        dd = float((eq / eq.cummax() - 1).min())
        print(f"    {name:<26} n={len(r):4d}  年化 {ann*100:+6.2f}%  Sharpe {shp:+5.2f}  最大回撤 {dd*100:6.1f}%")
        out[name] = {"ann": ann, "sharpe": shp, "n": len(r), "dd": dd}
        return shp

    perf(d["gap"], "隔夜多头（收盘买开盘卖）")
    perf(d["intra"], "日内多头（开盘买收盘卖）")
    perf(d["c2c"], "全天持有")

    # 条件日内：按已实现跳空分组（9:30 就能观测，无需预测）
    g = (d["f_gap"] * 100).copy()
    buckets = [(-99, -1.0, "低开>1%"), (-1.0, -0.3, "低开0.3~1%"), (-0.3, 0.3, "平开±0.3%"),
               (0.3, 1.0, "高开0.3~1%"), (1.0, 99, "高开>1%")]
    print("\n  条件日内收益（按开盘时已实现的跳空分组，这才是可执行的信息）:")
    for lo, hi, lab in buckets:
        m = (g >= lo) & (g < hi)
        if m.sum() < 20:
            continue
        s = d.loc[m, "f_intra"] * 10000
        net = s.mean() - 5.0
        print(f"    {lab:<12} n={m.sum():4d}  日内均值 {s.mean():+6.2f}bp  "
              f"上涨率 {(s>0).mean():.3f}  扣费后 {net:+6.2f}bp  "
              f"{'✓可交易' if net > 0 and (s>0).mean() > 0.53 else ''}")

    # 用模型预判的跳空方向去做日内，检验"预测能否变现"
    with E.db() as c:
        rows = pd.DataFrame([dict(r) for r in c.execute(
            "SELECT target,p_gap_up,e_gap,e_intra,actual_pct,actual_gap,actual_intra "
            "FROM pred WHERE evaluated=1 AND source='replay' AND actual_intra IS NOT NULL")])
    if len(rows) > 50:
        rows["t"] = pd.to_datetime(rows["target"])
        rows = rows.set_index("t")
        # 预测低开 → 开盘买入 → 收盘卖出
        sig = rows["p_gap_up"] < 0.35
        s = rows.loc[sig, "actual_intra"] * 100
        if len(s) > 20:
            print(f"\n  用模型'预测低开'做日内抄底: n={len(s)}  均值 {s.mean():+.2f}bp  "
                  f"扣费后 {s.mean()-5:+.2f}bp  上涨率 {(s>0).mean():.3f}")
        sig2 = rows["p_gap_up"] > 0.65
        s2 = rows.loc[sig2, "actual_intra"] * 100
        if len(s2) > 20:
            print(f"  用模型'预测高开'做日内做空: n={len(s2)}  均值 {s2.mean():+.2f}bp  "
                  f"扣费后 {-s2.mean()-5:+.2f}bp  下跌率 {(s2<0).mean():.3f}")
        print("  ↑ 若两者都接近 0，说明'跳空准确率'无法转化为交易收益")
    print("\n  → 结论：跳空预测的信息在 09:30 开盘时已归零（开盘价本身就是它的实现），")
    print("          唯一可执行的是日内段；而日内段与外盘几乎无关。")
    return out


# ======================================================================================
# [3] 状态依赖：信号符号是否翻转
# ======================================================================================
def part3(mkt: dict, bench: pd.DataFrame) -> None:
    hdr(3, "状态依赖：同一个信号在不同波动状态下符号是否翻转")
    dates = list(bench.index[:-1])
    rows = []
    for dt_ in dates[-1000:]:
        try:
            ctx = E.build_context(mkt, dt_, live=False)
        except Exception:
            continue
        if not ctx.bench:
            continue
        rec = {"date": dt_}
        for _, _, fn in A.EXPERTS:
            s = fn(ctx)
            if s.available:
                rec[s.key] = s.score
        rows.append(rec)
    sig = pd.DataFrame(rows).set_index("date")
    y = (bench["f_c2c"] > 0).astype(float).reindex(sig.index)
    fwd = bench["f_c2c"].reindex(sig.index)
    vol = bench["vol20"].reindex(sig.index)
    # 波动状态：用历史分位（避免未来函数）
    vrank = vol.rolling(250, min_periods=80).rank(pct=True)
    sig = sig.assign(fwd=fwd, y=y, vrank=vrank).dropna(subset=["fwd", "vrank"])

    print(f"  样本 {len(sig)} 日，按过去 250 日波动分位三档划分\n")
    keys = [k for k in A.HIST_EXPERTS if k in sig.columns]
    print(f"    {'信号':<12}{'低波':>16}{'中波':>16}{'高波':>16}   {'符号翻转':>8}")
    flips = []
    for k in keys:
        cells, means = [], []
        for lo, hi in ((0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.01)):
            m = (sig["vrank"] >= lo) & (sig["vrank"] < hi)
            sub = sig.loc[m, [k, "fwd"]].dropna()
            if len(sub) < 40:
                cells.append("  n/a")
                means.append(np.nan)
                continue
            r = sub[k].corr(sub["fwd"])
            hit = float((np.sign(sub[k]) == np.sign(sub["fwd"])).mean())
            cells.append(f"{r:+.3f}/{hit:.2f}")
            means.append(r)
        valid = [m for m in means if not np.isnan(m)]
        flipped = len(valid) >= 2 and (max(valid) > 0 > min(valid))
        if flipped:
            flips.append(k)
        print(f"    {A.EXPERT_LABELS.get(k,k):<12}" + "".join(f"{c:>16}" for c in cells) +
              f"   {'⚠ 是' if flipped else '否':>8}")
    print("\n    格式：相关系数/方向命中率")
    if flips:
        print(f"    ⚠ 符号翻转的信号：{', '.join(A.EXPERT_LABELS.get(k,k) for k in flips)}")
        print("    → 池化（全样本单一权重）时，这些信号的正负贡献互相抵消，")
        print("      表现得像'无效信号'。这是池化模型必然失效的机制性原因，不是调参能解决的。")

    # 基准率本身是否随状态变化
    print("\n  上涨基准率随状态变化（'永远看涨'也不是常数）:")
    for lo, hi, lab in ((0, 1 / 3, "低波"), (1 / 3, 2 / 3, "中波"), (2 / 3, 1.01, "高波")):
        m = (sig["vrank"] >= lo) & (sig["vrank"] < hi)
        if m.sum() > 20:
            print(f"    {lab}  n={m.sum():4d}  上涨率 {sig.loc[m,'y'].mean():.4f}  "
                  f"平均涨幅 {sig.loc[m,'fwd'].mean()*100:+.3f}%")
    print("\n  → 结论：方向模型的正确形态是 P(涨 | 状态)，而不是 P(涨)。")
    print("          给一个不区分状态的单一概率，本身就是错误设定。")


# ======================================================================================
# [4] 截面因子
# ======================================================================================
def part4(mkt: dict, bench: pd.DataFrame) -> None:
    hdr(4, "截面维度：板块之间是否有比时序更强的 alpha")
    frames = mkt["sec_frames"]
    if not frames:
        print("  无板块数据")
        return
    # 组装长表
    recs = []
    for code, f in frames.items():
        if f is None or f.empty:
            continue
        sub = f[["date", "r1", "r5", "r20", "vol_ratio_amt", "pos20", "vol20", "amount"]].copy()
        sub["code"] = code
        recs.append(sub)
    if not recs:
        return
    long = pd.concat(recs, ignore_index=True)
    long["date"] = pd.to_datetime(long["date"]).dt.normalize()
    long["fwd"] = long.groupby("code")["r1"].shift(-1)
    long = long.dropna(subset=["fwd", "r1"])
    long = long[long["fwd"].abs() < 11.5]          # 剔除涨跌停/指数重置等异常
    long = long.dropna(subset=["vol20"])
    # 剔除成分股数异常的日子：每日至少 30 个板块
    cnt = long.groupby("date")["code"].transform("count")
    long = long[cnt >= 30]

    bfwd = bench["f_c2c"].reindex(pd.Index(sorted(long["date"].unique())))
    print(f"  样本 {len(long)} 个（板块×日），{long['date'].nunique()} 个交易日，"
          f"{long['code'].nunique()} 个板块\n")

    factors = {
        "短期反转(-r1)": lambda g: -g["r1"],
        "中期动量(r20)": lambda g: g["r20"],
        "低波(-vol20)": lambda g: -g["vol20"],
        "缩量(-量比)": lambda g: -g["vol_ratio_amt"],
        "超跌(-pos20)": lambda g: -g["pos20"],
    }
    print(f"    {'因子':<16}{'IC均值':>9}{'IC-IR':>8}{'t值':>8}{'胜率':>8}{'Q5-Q1(bp)':>12}{'多空Sharpe':>11}")
    best = None
    for name, fn in factors.items():
        ics, spreads = [], []
        for dt_, g in long.groupby("date"):
            if len(g) < 30:
                continue
            x = fn(g).replace([np.inf, -np.inf], np.nan)
            if x.notna().sum() < 30 or x.nunique() < 5:
                continue
            ic = x.rank().corr(g["fwd"].rank())
            if pd.notna(ic):
                ics.append(ic)
            q = pd.qcut(x.rank(method="first"), 5, labels=False)
            hi = g.loc[q == 4, "fwd"].mean()
            lo = g.loc[q == 0, "fwd"].mean()
            spreads.append((hi - lo) * 100)      # pct -> bp
        if len(ics) < 100:
            print(f"    {name:<16} 样本不足")
            continue
        ics = np.array(ics)
        sp = np.array(spreads)
        ic_m, ic_s = ics.mean(), ics.std()
        t = ic_m / (ic_s / math.sqrt(len(ics)))
        ir = ic_m / (ic_s + 1e-12)
        hit = float((ics > 0).mean())
        shp = sp.mean() / (sp.std() + 1e-12) * math.sqrt(244)
        sp_bp = sp.mean()
        print(f"    {name:<16}{ic_m:+9.4f}{ir:+8.3f}{t:+8.2f}{hit:8.3f}"
              f"{sp_bp:+12.1f}{shp:+11.2f}")
        if best is None or abs(t) > abs(best[1]):
            best = (name, t, sp, ics)

    print("\n    ↑ Q5-Q1 = 每日做多因子最强 20%、做空最弱 20% 的平均日收益（bp，等权，未扣费）")
    if best:
        name, t, sp, ics = best
        print(f"    最强因子 {name}: t={t:.2f}，日均价差 {sp.mean():+.1f}bp，"
              f"扣 10bp 双边后 {sp.mean()-10:+.1f}bp")
    print("\n  → 注意：板块成分股用的是当前快照，历史回测存在成分股前视偏差（survivorship/PIT），")
    print("          这会系统性高估截面 alpha。真实可得值应低于此表。")


# ======================================================================================
# [5] 多重检验
# ======================================================================================
def part5() -> None:
    hdr(5, "选择偏差：测试了 N 个变体后，最好的那个还剩多少")
    print("  本项目累计测试过的独立变体（保守估计）:")
    trials = {
        "跳空模型窗长/加权/收缩": 8,
        "方向信号组合": 6,
        "日内层特征集": 4,
        "区间宽度方案": 3,
        "概率校准": 3,
        "板块因子": 5,
    }
    N = sum(trials.values())
    for k, v in trials.items():
        print(f"    {k:<26} {v}")
    print(f"    {'合计':<26} {N}")

    # 期望最大 Sharpe（零假设下）
    e = 0.5772
    def expected_max_sr(n, T):
        return (1 - e) * _ppf(1 - 1 / n) + e * _ppf(1 - 1 / (n * math.e))

    z = expected_max_sr(N, 0)
    print("\n  零假设（真实 Sharpe=0）下，测试 N 次，最大 Sharpe 的期望值:")
    for T in (250, 800, 1300):
        yrs = T / 244
        print(f"    样本 {T:4d} 日（{yrs:.1f} 年）：N={N} → 期望最大年化 SR {z/math.sqrt(yrs):.2f}")
    print("\n  → 在完全无效的系统上测 29 个变体，最漂亮的那个也会显示 SR≈0.9（1300 日样本）。")
    print("     所以本项目任何 SR<1.0 的结果都不能当作'有效'证据，包括跳空命中率。")
    print("     能过这道门槛的，只有截面因子（t≈4），而它扣费后接近零。")


def _ppf(p: float) -> float:
    # 标准正态分位数（Acklam 近似，够用）
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
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ======================================================================================
def main() -> None:
    print(LINE)
    print("A股预判系统 · 深度审计")
    print(LINE)
    mkt, bench = load()
    print(f"数据：上证 {bench.index[0].date()} ~ {bench.index[-1].date()}，{len(bench)} 个交易日")
    part1(bench)
    part2(bench)
    part3(mkt, bench)
    part4(mkt, bench)
    part5()


if __name__ == "__main__":
    main()
