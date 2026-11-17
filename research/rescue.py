"""护盘 / 救市信号的可验证性检验。

背景
----
网上关于「国家队救市」的判断方法，主流是两套：

1. 经验三条（雪球/公众号流传的"墨子"版本）
   ① 外部冲击引发恐慌杀跌（指数单日暴挫、大面积跌停）
   ② 监管放风（座谈会、汇金表态、再贷款）
   ③ 尾盘宽基 ETF 天量成交、银行保险托指数、指数跌幅收窄但个股仍绿
   三条同时成立才叫救市；只有口号没放量 = 普通反弹。

2. GitHub `zhangxianwen2/A-Resonance`（同频）——把判断做成五个灯：
   价格位置（ETF 60 日区间分位 ≤40 绿灯 / ≥70 红灯）
   份额流向（净申购绿灯 / 净赎回红灯）
   交易方向（低位放量吸筹绿灯 / 高位放量出货红灯）
   成交额热度（两市成交额分位 ≤20 绿灯 / ≥80 红灯）
   融资杠杆（融资余额分位 ≤20 绿灯 / ≥80 红灯）
   同色灯 ≥3 → 共振（绿灯=机会、红灯=危险）。

本脚本能验证什么
----------------
我们只有日线 OHLC + 成交额 + 板块日线 + ETF 当日快照，因此：

    可回测   价格位置、量能分位、收盘位置（尾盘拉回的日线代理）、
             权重相对强弱（沪深300 vs 中证1000）、银行板块超额
    不可回测 份额流向（etf_share 从首次运行才开始记录）、融资余额（未接入）、
             尾盘 30 分钟分时（无分钟数据）、监管放风（新闻无历史快照）

所以下面只测可回测的那几条，并明确标注哪些结论"无法验证"而不是"无效"。

用法
----
    python run.py research rescue
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from core import datahub as D
from core import engine as E
from core import depth as DP

COST_BP = 5.0          # 双边成本假设（与深度层一致）
HORIZONS = (1, 3, 5, 10)


# ======================================================================================
# 特征：全部只用当日及之前的信息
# ======================================================================================
def build_features(bench: pd.DataFrame, panel: pd.DataFrame,
                   bank: pd.Series | None,
                   margin: pd.DataFrame | None = None) -> pd.DataFrame:
    d = DP.prepare(bench)                      # 百分数口径的 gap / intra / c2c
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

    if bank is not None and not bank.empty:
        d["bank_ex"] = bank.reindex(d.index) - d["c2c"]
    else:
        d["bank_ex"] = np.nan

    # 两融：融资余额占流通市值比，取 250 日分位（越低=杠杆越冷）
    if margin is not None and not margin.empty:
        mm = margin.set_index("date")["rzye_pct"].reindex(d.index).ffill()
        d["margin_pct"] = mm
        d["margin_rank"] = mm.rolling(250).apply(
            lambda a: float((a[:-1] < a[-1]).mean()) if len(a) > 30 else np.nan, raw=True)

    # 前向收益：两种入场（收盘/次日开盘），全部为百分数
    for h in HORIZONS:
        d[f"f{h}"] = (d["close"].shift(-h) / d["close"] - 1) * 100
        d[f"o{h}"] = (d["close"].shift(-h) / d["open"].shift(-1) - 1) * 100
    return d


# ======================================================================================
# 事件定义
# ======================================================================================
def conditions(d: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        # ① 恐慌杀跌
        "恐慌": (d["c2c"] <= -2.0) | (d["dd5"] <= -4.0),
        # ② 放量：成交额处于近 250 日 80 分位以上
        "放量": d["amt_pct"] >= 0.80,
        # ③ 权重护盘：沪深300 明显强于中证1000（托指数、杀个股）
        "权重护盘": d["ex_300_1000"] >= 0.30,
        # ④ 低位：60 日区间下半部
        "低位": d["pos60"] <= 0.40,
        # ⑤ 尾盘拉回的日线代理：收阴但收在当日振幅上沿
        "长下影拉回": (d["c2c"] < 0) & (d["day_pos"] >= 0.65),
        # ⑥ 银行单独护盘
        "银行护盘": d["bank_ex"] >= 0.30,
        # ⑦ 融资杠杆冷清（余额占流通市值比的 250 日分位 ≤20）
        "杠杆低位": (d.get("margin_rank", pd.Series(index=d.index, dtype=float)) <= 0.20),
    }


# ======================================================================================
# 评估
# ======================================================================================
def tstat(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 5 or x.std() == 0:
        return float("nan")
    return float(x.mean() / (x.std() / math.sqrt(len(x))))


def evaluate(d: pd.DataFrame, mask: pd.Series, label: str, base: dict[int, float]) -> dict:
    m = mask.fillna(False)
    n = int(m.sum())
    out = {"label": label, "n": n}
    if n == 0:
        return out
    for h in HORIZONS:
        v = d.loc[m, f"f{h}"]
        o = d.loc[m, f"o{h}"]
        out[f"f{h}"] = round(float(v.mean()), 2)
        out[f"o{h}"] = round(float(o.mean()), 2)
        out[f"t{h}"] = round(tstat(v), 2)
        out[f"win{h}"] = round(float((v > 0).mean()), 3)
        out[f"ex{h}"] = round(float(v.mean() - base[h]), 2)     # 相对无条件均值
    return out


def main() -> None:
    mkt = E.load_market()
    bench = mkt["bench"]
    panel = mkt["panel"]

    # 银行板块日线（用于"银行护盘"变体）
    bank = None
    try:
        ind = D.sector_list("industry")
        row = ind[ind["name"] == "银行"]
        if not row.empty:
            kl = D.sector_klines([row.iloc[0]["code"]])
            for _c, f in kl.items():
                e = DP.prepare(f)
                bank = e["c2c"]
    except Exception as ex:  # noqa: BLE001
        print("  银行板块数据不可用:", ex)

    margin = None
    try:
        margin = D.margin_history(760)
        print(f"  两融：{len(margin)} 个交易日  "
              f"{margin['date'].min().date()} ~ {margin['date'].max().date()}")
    except Exception as ex:  # noqa: BLE001
        print("  两融数据不可用:", ex)

    d = build_features(bench, panel, bank, margin).dropna(subset=["pos60", "f1"])
    print(f"\n  样本 {len(d)} 个交易日  {d.index[0].date()} ~ {d.index[-1].date()}")

    base = {h: float(d[f"f{h}"].mean()) for h in HORIZONS}
    print("  无条件前向均值：" + "  ".join(f"T+{h} {base[h]:+.2f}%" for h in HORIZONS))

    cond = conditions(d)
    print("\n  单个条件触发频率：")
    for k, v in cond.items():
        print(f"    {k:<8} {int(v.fillna(False).sum()):4d} 次  ({v.fillna(False).mean() * 100:4.1f}%)")

    # ---- 组合 ----
    combos = {
        "墨子三条（恐慌+放量+权重护盘）": cond["恐慌"] & cond["放量"] & cond["权重护盘"],
        "同频三灯（低位+放量+拉回）": cond["低位"] & cond["放量"] & cond["长下影拉回"],
        "放宽（(恐慌或低位)+放量+护盘）": (cond["恐慌"] | cond["低位"]) & cond["放量"] & cond["权重护盘"],
        "仅恐慌+放量": cond["恐慌"] & cond["放量"],
        "仅低位+放量": cond["低位"] & cond["放量"],
        "银行护盘+放量": cond["银行护盘"] & cond["放量"],
        "救市三条+杠杆低位": cond["恐慌"] & cond["放量"] & cond["权重护盘"] & cond["杠杆低位"],
        "恐慌+杠杆低位": cond["恐慌"] & cond["杠杆低位"],
    }

    print("\n  组合信号（入场=信号日收盘；ex=相对无条件均值）")
    print(f"    {'组合':<30}{'n':>4}{'T+1':>8}{'ex':>7}{'t':>6}"
          f"{'T+5':>8}{'ex':>7}{'t':>6}{'胜率':>7}")
    rows = []
    for label, mask in combos.items():
        r = evaluate(d, mask, label, base)
        rows.append(r)
        if r["n"] == 0:
            print(f"    {label:<30}{0:>4}   从未触发")
            continue
        print(f"    {label:<30}{r['n']:>4}{r['f1']:>8.2f}{r['ex1']:>7.2f}"
              f"{r['t1']:>6.2f}{r['f5']:>8.2f}{r['ex5']:>7.2f}{r['t5']:>6.2f}"
              f"{r['win5'] * 100:>6.0f}%")

    # ---- 逐个事件列出，便于肉眼核对（是否与真实救市节点吻合） ----
    print("\n  「墨子三条」触发的具体日期（含前向 5 日收益）：")
    m = combos["墨子三条（恐慌+放量+权重护盘）"].fillna(False)
    for dt, row in d[m].tail(12).iterrows():
        print(f"    {dt.date()}  当日{row['c2c']:+.2f}%  "
              f"位置{row['pos60']:.2f}  成交额分位{row['amt_pct']:.2f}  "
              f"300-1000 {row['ex_300_1000']:+.2f}  T+5 {row['f5']:+.2f}%")

    # ---- 份额/融资/尾盘：说明为何无法验证 ----
    try:
        with E.db() as c:
            days = c.execute("SELECT COUNT(DISTINCT day) FROM etf_share").fetchone()[0]
    except Exception:  # noqa: BLE001
        days = 0
    if "margin_rank" in d:
        cov = int(d["margin_rank"].notna().sum())
        print(f"\n  注：杠杆条件仅有 {cov} 个交易日可用（两融历史 760 日），"
              f"相关组合的样本显著小于全样本。")

    print("\n  仍无法检验的部分（样本不足，不等于无效）：")
    print(f"    · ETF 份额：日频仅本地累积 {days} 日；季度口径（f10 规模变动）只能做状态描述，")
    print("      且 2026-09 四只宽基全是净申购，横截面上没有区分度，无法做因子。")
    print("    · 融资杠杆：历史 551 个交易日，叠加'恐慌+放量'后只剩 2 次事件，")
    print("      点估计 +1.70%/次日 但 n=2，不能作为证据。")
    print("    · 尾盘 30 分钟：东财 5 分钟线只给 32 个交易日且无法翻页，不足以检验。")
    print("    · 监管放风（座谈会/汇金表态）：新闻无历史快照，无法回溯。")


if __name__ == "__main__":
    main()
