"""护盘 / 国家队共振检测。

借自 GitHub `zhangxianwen2/A-Resonance`（同频）的多灯框架：每个指标亮一盏灯，
同色灯 ≥3 判定共振。原项目做在 ETF 上，这里移植到指数 + 板块 + ETF 份额。

五盏灯里只有三盏有历史数据，另外两盏如实标灰——这一点很关键：
A-Resonance 的"ETF 资金"和"融资杠杆"我们都没有历史，标成"未接入"比
编一个数字更像样，也避免用假信号去凑共振。

另外补一条本仓库回测过的信号（research/rescue.py）：
    恐慌杀跌 & 放量 & 权重护盘（沪深300 强于中证1000）
在 1240 个交易日里触发 11 次，次日 +1.40%（t=5.51）、5 日 +1.83%、胜率 73%，
触发日对上 2024-02-05、2025-04-07、2026-03-23 等真实托底节点。
样本小（11 次），所以只作为"提示"，不进入概率模型。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import depth as DP
from . import market as MK
from . import store as ST

# 宽基 ETF：汇金/国新主要借道这些
WIDE_ETF = ("510300", "510050", "510500", "512100", "588000", "159919")

# 与 research/rescue.py 保持一致的回测结论（样本内，仅作提示用）
RESCUE_STATS = {"n": 11, "f1": 1.40, "t1": 5.51, "f5": 1.83, "win5": 0.73,
                "span": "2021-09 ~ 2026-11"}


def _light(key: str, name: str, state: str, value: str, note: str) -> dict:
    return {"key": key, "name": name, "state": state, "value": value, "note": note}


def _features(bench: pd.DataFrame, panel: pd.DataFrame) -> pd.DataFrame:
    d = DP.prepare(bench)
    d["pos60"] = (d["close"] - d["close"].rolling(60).min()) / (
        d["close"].rolling(60).max() - d["close"].rolling(60).min() + 1e-9)
    d["amt_pct"] = d["amount"].rolling(250).apply(
        lambda s: float((s[:-1] < s[-1]).mean()) if len(s) > 20 else np.nan, raw=True)
    rng = (d["high"] - d["low"]).replace(0, np.nan)
    d["day_pos"] = ((d["close"] - d["low"]) / rng).clip(0, 1)
    d["dd5"] = d["c2c"].rolling(5).sum()
    if panel is not None and not panel.empty and {"沪深300", "中证1000"} <= set(panel.columns):
        d["ex_300_1000"] = panel["沪深300"] - panel["中证1000"]
    else:
        d["ex_300_1000"] = np.nan
    return d


def _share_light(mkt: dict) -> tuple[dict, int]:
    """宽基 ETF 份额：净申购为绿（吸筹），净赎回为红。

    日频份额没有公开历史接口，只能本机逐日累积（etf_share 表）；
    样本不足两天时退回 **季度口径**：f10 的"期间申购 / 期间赎回 / 期末份额"，
    用 (申购-赎回)/期末份额 表示这一期的净申赎强度。
    """
    try:
        chg = MK.etf_share_changes(max_days=5)
        with ST.db() as c:
            days = c.execute("SELECT COUNT(DISTINCT day) FROM etf_share").fetchone()[0]
    except Exception:  # noqa: BLE001
        chg, days = {}, 0

    vals = [chg[c] for c in WIDE_ETF if c in chg]
    if len(vals) >= 2:
        avg = float(np.mean(vals))
        if avg >= 0.5:
            return _light("share", "ETF 资金", "green", f"{avg:+.2f}%", "大盘 ETF 被净申购，资金在进"), days
        if avg <= -0.5:
            return _light("share", "ETF 资金", "red", f"{avg:+.2f}%", "大盘 ETF 被净赎回，资金在出"), days
        return _light("share", "ETF 资金", "grey", f"{avg:+.2f}%", "ETF 份额基本没变"), days

    # 退回季度口径
    hist = mkt.get("etf_share_hist") or {}
    ratios, names = [], []
    for code, df in hist.items():
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        if not last.get("shares") or last["shares"] <= 0:
            continue
        net = (float(last["buy"] or 0) - float(last["sell"] or 0)) / float(last["shares"])
        ratios.append(net)
        names.append(code)
    if not ratios:
        return _light("share", "ETF 资金", "grey", "—", "暂无份额数据"), days

    med = float(np.median(ratios))
    per = str(pd.to_datetime(hist[names[0]].iloc[-1]["date"]).date())[:7]
    if med >= 0.02:
        return _light("share", "ETF 资金", "green", f"{med * 100:+.1f}%",
                      f"{per} 这季 {len(ratios)} 只大盘 ETF 净申购"), days
    if med <= -0.02:
        return _light("share", "ETF 资金", "red", f"{med * 100:+.1f}%",
                      f"{per} 这季 {len(ratios)} 只大盘 ETF 净赎回"), days
    return _light("share", "ETF 资金", "grey", f"{med * 100:+.1f}%",
                  "上季度 ETF 份额基本没变"), days


def _margin_light(mkt: dict) -> dict:
    """融资杠杆：融资余额占流通市值的分位。冷清=机会（绿），过热=风险（红）。"""
    m = mkt.get("margin")
    if m is None or getattr(m, "empty", True) or len(m) < 60:
        return _light("margin", "杠杆资金", "grey", "—", "两融数据暂不可用")
    s = pd.to_numeric(m["rzye_pct"], errors="coerce").dropna()
    if len(s) < 60:
        return _light("margin", "杠杆资金", "grey", "—", "两融样本不足")
    cur = float(s.iloc[-1])
    win = s.iloc[-250:]
    rank = float((win.iloc[:-1] < cur).mean()) if len(win) > 20 else float("nan")
    last = m.iloc[-1]
    rzye_yi = float(last.get("rzye") or 0) / 1e12
    jme = float(last.get("rzjme") or 0) / 1e8
    note = f"余额 {rzye_yi:.2f} 万亿 · 净买入 {jme:+.0f} 亿"
    if rank <= 0.20:
        return _light("margin", "杠杆资金", "green", "偏低", f"近一年 {rank:.0%} 分位 · " + note)
    if rank >= 0.80:
        return _light("margin", "杠杆资金", "red", "偏高", f"近一年 {rank:.0%} 分位 · " + note)
    return _light("margin", "杠杆资金", "grey", "中性", f"近一年 {rank:.0%} 分位 · " + note)


def snapshot(mkt: dict, asof: pd.Timestamp | None = None) -> dict:
    bench = mkt["bench_raw"]
    bench = bench[pd.to_datetime(bench["date"]).dt.normalize() <= pd.Timestamp(asof)] \
        if asof is not None else bench
    d = _features(bench, mkt["panel"]).dropna(subset=["pos60"])
    if d.empty:
        return {"error": "无足够历史数据"}
    last = d.iloc[-1]
    pos = float(last["pos60"])
    # 成交额分位可能是 NaN：备用数据源不返回成交额，rolling 出来的整列都是 NaN。
    # 直接 float() 会得到 nan，再被 f"{heat:.0%}" 拼成界面上刺眼的 "nan%"；
    # 更糟的是 nan 参与比较一律为 False，会一路落进"正常"分支——
    # 把"没有数据"说成"正常"，这是最不该出现的一种误导。
    heat = float(last["amt_pct"]) if pd.notna(last["amt_pct"]) else float("nan")
    has_heat = heat == heat  # 非 NaN

    def _heat_txt() -> str:
        return f"成交额处近一年 {heat:.0%} 分位" if has_heat else "成交额缺失，无法判断冷热"

    pct, daypos = float(last["c2c"]), float(last["day_pos"])
    ex = float(last["ex_300_1000"]) if pd.notna(last["ex_300_1000"]) else 0.0
    vol_up = has_heat and heat >= 0.70

    lights = []

    # 1. 价格位置
    if pos <= 0.40:
        lights.append(_light("pos", "指数位置", "green", "偏低", f"近 3 个月区间 {pos:.0%} 处"))
    elif pos >= 0.70:
        lights.append(_light("pos", "指数位置", "red", "偏高", f"近 3 个月区间 {pos:.0%} 处"))
    else:
        lights.append(_light("pos", "指数位置", "grey", "居中", f"近 3 个月区间 {pos:.0%} 处"))

    # 2. 份额流向（数据不足时如实标灰）
    sh, share_days = _share_light(mkt)
    lights.append(sh)

    # 3. 交易方向：低位放量吸筹 / 高位放量出货
    if vol_up and daypos >= 0.60 and pos <= 0.45:
        lights.append(_light("dir", "量价配合", "green", "吸筹", f"低位放量，收在当天高位（{_heat_txt()}）"))
    elif vol_up and daypos <= 0.40 and pos >= 0.65:
        lights.append(_light("dir", "量价配合", "red", "出货", f"高位放量，收在当天低位（{_heat_txt()}）"))
    else:
        lights.append(_light("dir", "量价配合", "grey", "—", "量价没有一致信号"))

    # 4. 成交额热度（市场情绪，方向与"位置"相反）
    if not has_heat:
        lights.append(_light("heat", "成交热度", "grey", "无数据", _heat_txt()))
    elif heat <= 0.20:
        lights.append(_light("heat", "成交热度", "green", "清淡", _heat_txt()))
    elif heat >= 0.80:
        lights.append(_light("heat", "成交热度", "red", "过热", _heat_txt()))
    else:
        lights.append(_light("heat", "成交热度", "grey", "正常", _heat_txt()))

    # 5. 融资杠杆（未接入）
    lights.append(_margin_light(mkt))

    green = sum(1 for x in lights if x["state"] == "green")
    red = sum(1 for x in lights if x["state"] == "red")
    if green >= 3:
        verdict, vkey = "资金面偏暖", "green"
    elif red >= 3:
        verdict, vkey = "资金面偏冷", "red"
    else:
        verdict, vkey = "中性", "grey"

    # ---- 本项目回测过的"救市三条" ----
    panic = bool(pct <= -2.0 or float(last["dd5"]) <= -4.0)
    heavy = bool(has_heat and heat >= 0.80)
    support = bool(ex >= 0.30)
    triggered = panic and heavy and support

    tail = mkt.get("tail30")
    tail_info = None
    if tail is not None and not getattr(tail, "empty", True):
        t = tail.iloc[-1]
        tail_info = {"date": str(t["date"])[:10], "pct": float(t["pct"]),
                     "mean": float(tail["pct"].mean()), "n": int(len(tail)),
                     "win": float((tail["pct"] > 0).mean())}

    hist = d[(d["c2c"] <= -2.0) | (d["dd5"] <= -4.0)]
    hist = hist[(hist["amt_pct"] >= 0.80) & (hist["ex_300_1000"] >= 0.30)]
    events = [{"date": str(i.date()), "pct": round(float(r["c2c"]), 2),
               "pos": round(float(r["pos60"]), 2)}
              for i, r in hist.tail(6).iterrows()]

    return {
        "asof": str(d.index[-1].date()),
        "lights": lights,
        "green": green, "red": red,
        "verdict": verdict, "verdict_key": vkey,
        "rescue": {
            "triggered": triggered,
            "conds": [{"name": "恐慌下跌", "on": panic, "detail": f"当天 {pct:+.2f}%"},
                      {"name": "成交放大", "on": heavy, "detail": _heat_txt()},
                      {"name": "大盘股护盘", "on": support,
                       "detail": f"大盘股比小盘股强 {ex:+.2f}%"}],
            "stats": RESCUE_STATS,
            "events": events,
        },
        "tail30": tail_info,
        "notes": {
            "share_days": share_days,
            "unverifiable": ["尾盘 30 分钟（仅 32 日，不足以检验）",
                             "监管放风（新闻无历史快照）"],
            "source": "框架借自 zhangxianwen2/A-Resonance；阈值与第三条信号按本项目样本校准",
        },
    }
