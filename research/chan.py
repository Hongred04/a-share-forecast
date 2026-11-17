"""缠论可行性验证：这些买卖点到底能不能预测？

要回答的问题
    1. 上证指数日线上的缠论买点，次日开盘买入后 1/3/5/10 日是否有超额？
    2. 卖点是否对应下跌？如果卖点不跌，那这套结构只是"事后画图"。
    3. 结论对参数（笔的最小跨度、背驰阈值）敏感吗？换一组参数就翻转的结论没有价值。
    4. 样本量够不够支撑任何结论？

口径（必须严格遵守，否则整套结果作废）
    · 信号在 confirmed_at **收盘后**才算确认，所以一律从 confirmed_at+1 的开盘价入场。
      用信号当天的收盘价入场，等于假设你能在收盘那一刻知道这笔已经封口——偷看。
    · 收益用百分数，与 research/ 其他脚本一致。
    · 基准用同一段样本内所有交易日的同口径 N 日收益，不是"零"。
      A 股长期漂移为正，拿 0 当基准会把"市场本身在涨"算成信号的功劳。

用法
    python research/chan.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core import chan as CH  # noqa: E402
from core import datahub as D  # noqa: E402

HORIZONS = (1, 3, 5, 10)


def _fwd_returns(df: pd.DataFrame, idx: int) -> dict[int, float] | None:
    """从 idx+1 开盘入场，持有 N 日到收盘的收益（%）。越界返回 None。"""
    entry_i = idx + 1
    if entry_i >= len(df):
        return None
    entry = float(df["open"].iloc[entry_i])
    if not np.isfinite(entry) or entry <= 0:
        return None
    out = {}
    for n in HORIZONS:
        j = entry_i + n - 1
        if j >= len(df):
            return None
        out[n] = (float(df["close"].iloc[j]) / entry - 1) * 100
    return out


def _baseline(df: pd.DataFrame) -> dict[int, float]:
    """全样本同口径 N 日收益均值——这才是"什么都不做"的基准。"""
    base = {n: [] for n in HORIZONS}
    for i in range(0, len(df) - max(HORIZONS) - 2, 5):   # 每 5 日取一个样本，降低重叠
        r = _fwd_returns(df, i)
        if r:
            for n in HORIZONS:
                base[n].append(r[n])
    return {n: float(np.mean(v)) if v else float("nan") for n, v in base.items()}


def _stats(vals: list[float]) -> dict:
    a = np.array(vals, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    n = a.size
    mean = float(a.mean())
    sd = float(a.std(ddof=1)) if n > 1 else float("nan")
    t = mean / (sd / np.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    return {"n": n, "mean": mean, "median": float(np.median(a)),
            "win": float((a > 0).mean()), "t": t}


# ---------------------------------------------------------------------------
# 三个"别骗自己"的检验
# ---------------------------------------------------------------------------
def confirm_delay(df: pd.DataFrame, res: CH.ChanResult) -> pd.DataFrame:
    """信号确认时点距离它所指的那笔结束，隔了多少根 K 线。

    缠论信号天生滞后：一笔要等下一笔起来才封口。如果这个延迟中位数是十几根，
    那"1 日收益"根本没有交易含义——确认时行情早已走完，你买到的是半山腰。
    这个数字必须摆在最前面，否则后面所有收益率都在误导。
    """
    rows = []
    for s in res.signals:
        bi = res.bis[s.bi_idx] if 0 <= s.bi_idx < len(res.bis) else None
        if bi is None:
            continue
        rows.append({"kind": s.kind, "delay": s.confirmed_at - bi.b.raw_idx})
    return pd.DataFrame(rows)


def leave_one_out(vals: list[float]) -> dict:
    """留一法：每次剔掉一个样本，看均值是否还成立。

    14 个样本 + 一个 +17% 的极端值，就足以让均值好看得不像话。
    如果剔掉单个样本结论就翻转，那这个结论属于那一个样本，不属于这套方法。
    """
    a = np.array([v for v in vals if np.isfinite(v)], dtype=float)
    if a.size < 3:
        return {"n": a.size}
    loo = [float(np.delete(a, i).mean()) for i in range(a.size)]
    return {"n": a.size, "full": float(a.mean()),
            "worst": float(min(loo)), "best": float(max(loo)),
            "sign_flip": bool(min(loo) <= 0 < max(loo))}


def permutation_test(df: pd.DataFrame, sig_idx: list[int], horizon: int,
                     side: str, n_iter: int = 5000, seed: int = 7) -> dict:
    """置换检验：随机挑同样多的日子，看有多少比例能达到信号组的效果。

    这是回答"是不是运气"的唯一办法。t 值在 n=14 时靠不住，
    但如果 5000 次随机抽样里只有 1% 能达到 +1.39%，那至少说明这不是随手可得。
    """
    pool = [i for i in range(len(df) - max(HORIZONS) - 2)
            if _fwd_returns(df, i)]
    if len(sig_idx) < 3 or not pool:
        return {"n": len(sig_idx)}

    def val(i: int) -> float:
        r = _fwd_returns(df, i)
        return r[horizon] if r else float("nan")

    obs = float(np.nanmean([val(i) for i in sig_idx]))
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n_iter):
        samp = rng.choice(pool, size=len(sig_idx), replace=False)
        m = float(np.nanmean([val(int(i)) for i in samp]))
        if (m >= obs) if side == "buy" else (m <= obs):
            hits += 1
    return {"obs": obs, "p": hits / n_iter, "n": len(sig_idx)}


def run(min_gap: int, beichi_ratio: float, label: str, df: pd.DataFrame,
        base: dict[int, float]) -> dict:
    r = CH.analyze(df, min_gap=min_gap)
    # 覆盖阈值：analyze 内部用的是默认 BEICHI_RATIO，这里重算一次以支持敏感性分析
    if beichi_ratio != CH.BEICHI_RATIO:
        _, _, hist = CH.macd_series(df["close"])
        r.signals = CH.find_signals(r.bis, r.zhongshus, hist.to_numpy(dtype=float),
                                    beichi_ratio=beichi_ratio)

    buys = {"buy1", "buy2", "buy3", "pzbuy"}
    sells = {"sell1", "sell2", "sell3", "pzsell"}

    rows = []
    for s in r.signals:
        fwd = _fwd_returns(df, s.confirmed_at)
        if not fwd:
            continue
        rows.append({"kind": s.kind, "side": "buy" if s.kind in buys else "sell",
                     "date": str(df["date"].iloc[s.confirmed_at])[:10],
                     **{f"r{n}": fwd[n] for n in HORIZONS}})
    detail = pd.DataFrame(rows)

    out = {"label": label, "n_sig": len(r.signals), "n_used": len(detail),
           "bis": len(r.bis), "zs": len(r.zhongshus), "detail": detail}

    for side in ("buy", "sell"):
        sub = detail[detail["side"] == side] if not detail.empty else detail
        for n in HORIZONS:
            key = f"{side}_{n}"
            if sub.empty:
                out[key] = {"n": 0}
            else:
                st = _stats(sub[f"r{n}"].tolist())
                st["excess"] = st.get("mean", float("nan")) - base[n]
                out[key] = st
    return out


def main() -> None:
    df = D.kline("1.000001", 1300).reset_index(drop=True)
    if df.empty:
        print("拉不到指数日线，无法验证")
        return
    df = df[["date", "open", "high", "low", "close"]].copy()
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    print(f"样本：{str(df['date'].iloc[0])[:10]} ~ {str(df['date'].iloc[-1])[:10]}，"
          f"{len(df)} 个交易日")
    base = _baseline(df)
    print("基准（全样本等权，%）：" + "  ".join(f"{n}日 {base[n]:+.2f}" for n in HORIZONS))
    print()

    configs = [
        (4, 0.9, "默认（老笔 min_gap=4，背驰 0.9）"),
        (3, 0.9, "新笔 min_gap=3"),
        (4, 1.0, "背驰放宽到 1.0"),
        (3, 1.0, "最宽松（3 / 1.0）"),
    ]
    results = []
    for gap, ratio, label in configs:
        res = run(gap, ratio, label, df, base)
        results.append(res)
        print(f"── {label}")
        print(f"   笔 {res['bis']}  中枢 {res['zs']}  信号 {res['n_sig']}（可用 {res['n_used']}）")
        for side, name in (("buy", "买点"), ("sell", "卖点")):
            cells = []
            for n in HORIZONS:
                st = res[f"{side}_{n}"]
                if st.get("n", 0) == 0:
                    cells.append(f"{n}日 —")
                    continue
                cells.append(f"{n}日 {st['mean']:+.2f}%(超额{st['excess']:+.2f},t={st['t']:+.1f},胜{st['win']:.0%})")
            print(f"   {name}：" + "  ".join(cells))
        print()

    # 汇总：默认参数下的逐条信号
    d = results[0]["detail"]
    if not d.empty:
        print("默认参数下的全部信号：")
        with pd.option_context("display.width", 120):
            print(d.to_string(index=False, float_format=lambda x: f"{x:+.2f}"))

    # ---------------- 三个检验 ----------------
    print()
    print("═" * 66)
    print("检验一：信号确认时距它所指的那笔结束，隔了多少根 K 线")
    res0 = run(4, CH.BEICHI_RATIO, "默认", df, base)
    # run() 没返回结构体，重新算一份用于延迟统计
    r0 = CH.analyze(df, min_gap=4)
    dl = confirm_delay(df, r0)
    if not dl.empty:
        for side, name in (("buy", "买点"), ("sell", "卖点")):
            sub = dl[dl["kind"].isin(["buy1", "buy2", "buy3", "pzbuy"])] if side == "buy" \
                else dl[dl["kind"].isin(["sell1", "sell2", "sell3", "pzsell"])]
            if len(sub):
                print(f"   {name}延迟：中位 {int(sub['delay'].median())} 根，"
                      f"均值 {sub['delay'].mean():.1f} 根，最大 {int(sub['delay'].max())} 根（n={len(sub)}）")

    print()
    print("检验二：留一法——剔掉任意单个信号，结论还在不在")
    for side, name in (("buy", "买点"), ("sell", "卖点")):
        vals = d[d["side"] == side]["r3"].tolist() if not d.empty else []
        lo = leave_one_out(vals)
        if lo.get("n", 0) >= 3:
            flag = "会翻转 → 结论由个别样本决定" if lo["sign_flip"] else "不翻转"
            print(f"   {name} 3日：全样本 {lo['full']:+.2f}%，"
                  f"最差 {lo['worst']:+.2f}%，最好 {lo['best']:+.2f}%  → {flag}")

    print()
    print("检验三：置换检验——随机挑同样多的日子，有多少比例能达到这个效果")
    buys_idx = [s.confirmed_at for s in r0.signals if s.kind in {"buy1", "buy2", "buy3", "pzbuy"}]
    sells_idx = [s.confirmed_at for s in r0.signals if s.kind in {"sell1", "sell2", "sell3", "pzsell"}]
    for side, idx, name in (("buy", buys_idx, "买点"), ("sell", sells_idx, "卖点")):
        for h in (3, 5):
            pt = permutation_test(df, idx, h, side)
            if pt.get("n", 0) >= 3:
                verdict = "不像随机" if pt["p"] <= 0.05 else ("边缘" if pt["p"] <= 0.10 else "与随机无法区分")
                print(f"   {name} {h}日：观测 {pt['obs']:+.2f}%，p={pt['p']:.3f}（n={pt['n']}）→ {verdict}")

    print()
    print("结论要点：")
    print(f"  · 5.3 年只出了 {results[0]['n_sig']} 个信号，买点样本 "
          f"{results[0]['buy_5'].get('n', 0)} 个——这个量级下任何 t 值都不可信。")
    print("  · 参数一换（min_gap 4→3 或背驰 0.9→1.0）信号数变化，看结论是否翻转。")


if __name__ == "__main__":
    main()
