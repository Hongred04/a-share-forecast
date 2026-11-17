"""因子研究脚本（一次性分析，不参与线上推理）。"""
import numpy as np
import pandas as pd

from core import analytics as A
from core import datahub as D


def main() -> None:
    idx = D.index_klines(1300)
    bench = A.enrich(idx["上证指数"])
    panel = A.align_panel(idx, "pct")

    ov = D.overseas_klines(1300)
    ovdf = pd.DataFrame({k: A.enrich(v).set_index("date")["r1"] for k, v in ov.items() if not v.empty})
    w = {"纳斯达克": 0.34, "标普500": 0.26, "道琼斯": 0.16, "恒生指数": 0.24}
    ovw = sum(ovdf[k].fillna(0) * v for k, v in w.items() if k in ovdf) / sum(w.values())

    b = bench.set_index("date")
    b["fwd"] = b["r1"].shift(-1)
    b["ov"] = ovw.reindex(b.index)
    b["spread_small"] = (panel["中证1000"] - panel["沪深300"]).reindex(b.index)
    b["spread_growth"] = (panel["创业板指"] - panel["上证指数"]).reindex(b.index)
    b["up_idx_share"] = (panel > 0).sum(axis=1).reindex(b.index) / panel.shape[1]
    b["med_idx"] = panel.median(axis=1).reindex(b.index)
    b["overnight_gap"] = (b["open"] / b["close"].shift(1) - 1) * 100
    b["intraday"] = (b["close"] / b["open"] - 1) * 100
    b["dow"] = b.index.dayofweek
    b["dom"] = b.index.day

    feats = {
        "反向1日": -b["r1"],
        "反向3日": -b["r3"],
        "反向5日": -b["r5"],
        "正向5日": b["r5"],
        "正向20日": b["r20"],
        "MA20乖离": b["dev20"],
        "MA20乖离反向": -b["dev20"],
        "RSI6": b["rsi6"] - 50,
        "RSI6反向": -(b["rsi6"] - 50),
        "区间位置": b["pos20"] - 0.5,
        "区间位置反向": -(b["pos20"] - 0.5),
        "外盘": b["ov"],
        "外盘x当日": b["ov"] * np.sign(b["r1"]),
        "量比": b["vol_ratio_amt"],
        "量比x方向": b["vol_ratio_amt"] * np.sign(b["r1"]),
        "ATR": b["atr_pct"],
        "连涨跌反向": -b["consec"].clip(-4, 4),
        "小盘超额1日": b["spread_small"],
        "小盘超额1日反向": -b["spread_small"],
        "成长超额1日": b["spread_growth"],
        "指数上涨占比": b["up_idx_share"],
        "指数中位涨幅": b["med_idx"],
        "跳空": b["overnight_gap"],
        "跳空反向": -b["overnight_gap"],
        "日内涨幅": b["intraday"],
        "日内反向": -b["intraday"],
        "MACD": b["macd"],
    }

    print("样本区间 %s ~ %s   N=%d" % (b.index[0].date(), b.index[-1].date(), b["fwd"].notna().sum()))
    print("次日上涨概率基准 %.3f   次日均值 %+.3f%%\n" % ((b["fwd"] > 0).mean(), b["fwd"].mean()))
    rows = []
    for k, v in feats.items():
        s = pd.DataFrame({"x": pd.Series(v).astype(float), "y": b["fwd"]}).dropna()
        if len(s) < 200:
            continue
        c = s["x"].corr(s["y"])
        t = c * np.sqrt(len(s) - 2) / np.sqrt(max(1 - c * c, 1e-9))
        half = len(s) // 2
        c1 = s["x"].iloc[:half].corr(s["y"].iloc[:half])
        c2 = s["x"].iloc[half:].corr(s["y"].iloc[half:])
        rows.append((k, c, t, c1, c2, len(s), (np.sign(s["x"]) == np.sign(s["y"])).mean()))
    df = pd.DataFrame(rows, columns=["因子", "IC", "t值", "前半IC", "后半IC", "N", "方向命中"])
    df["稳定"] = np.sign(df["前半IC"]) == np.sign(df["后半IC"])
    df = df.sort_values("t值", key=lambda x: -x.abs())
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(df.round(3).to_string(index=False))

    # 组合测试：稳健正向因子等权
    picks = df[(df["稳定"]) & (df["t值"].abs() > 2.0)]
    print("\n稳定且 |t|>2 的因子：", list(picks["因子"]))
    if len(picks):
        # 各因子可能建立在不同索引上，先统一对齐到基准日，否则会得到全 NaN 的合成
        z = pd.DataFrame({k: pd.Series(feats[k]).reindex(b.index) for k in picks["因子"]})
        z = (z - z.rolling(250, min_periods=60).mean()) / (z.rolling(250, min_periods=60).std() + 1e-9)
        # 注意：z 是日期索引、符号是因子名索引，直接相乘会按索引对齐成 NaN。
        # 正确做法是按列定向（.mul(axis=1)），再横向平均。
        signs = np.sign(picks.set_index("因子")["IC"].reindex(z.columns).to_numpy())
        comp = z.clip(-3, 3).mul(signs, axis=1).mean(axis=1)
        sig = pd.DataFrame({"c": comp, "y": b["fwd"]}).dropna()
        if len(sig) < 60:
            print("等权合成：样本不足（%d 行），跳过——先看上面的单因子 t 值" % len(sig))
            return
        print("等权合成 IC=%.3f  t=%.2f  命中=%.3f  N=%d" % (
            sig["c"].corr(sig["y"]),
            sig["c"].corr(sig["y"]) * np.sqrt(len(sig) - 2) / np.sqrt(max(1 - sig["c"].corr(sig["y"]) ** 2, 1e-9)),
            (np.sign(sig["c"]) == np.sign(sig["y"])).mean(), len(sig)))


if __name__ == "__main__":
    main()
