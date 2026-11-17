"""缠论几何不变量测试（离线，不依赖网络）。

为什么只测不变量、不测"结果对不对"
    缠论没有唯一正确答案，不同人画同一段 K 线能得出不同的笔。
    能测的只有**结构必须满足的几何关系**：合并后的 K 线之间不能再有包含、
    笔必须顶底交替、中枢上沿必须高于下沿、确认时点不能早于信号所指的位置。
    这些一旦破了，说明实现错了，而不是"流派不同"。

    用合成数据而不是真实行情：测试不该依赖网络。真实数据上的统计
    放在 research/chan.py，那是研究不是单元测试。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core import chan as CH  # noqa: E402

bad = 0


def ok(cond, msg, detail=""):
    global bad
    if cond:
        print(f"✓ {msg}")
    else:
        bad += 1
        print(f"✗ {msg}" + (f"  [{detail}]" if detail else ""))


def synth(n=400, periods=(37, 91), amps=(1.6, 3.2), drift=0.02, seed=3):
    """合成一段有明显波段的价格序列，用来验证结构识别。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    level = 100 + drift * t
    for p, a in zip(periods, amps):
        level = level + a * np.sin(2 * np.pi * t / p)
    level = level + rng.normal(0, 0.25, n).cumsum() * 0.15
    close = np.maximum(level, 1.0)
    # 用上一根收盘做开盘，造出合理的 OHLC
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.05, 0.45, n)
    low = np.minimum(open_, close) - rng.uniform(0.05, 0.45, n)
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=n),
        "open": open_, "high": high, "low": low, "close": close,
    })


df = synth()
r = CH.analyze(df)

# ---------------------------------------------------------------- 包含处理
ok(len(r.bars) > 20, "合成数据能识别出足够的合并 K 线", str(len(r.bars)))
ok(len(r.bars) <= len(df), "合并后数量不能超过原始数量")

viol = 0
for a, b in zip(r.bars, r.bars[1:]):
    if CH._included(a.high, a.low, b.high, b.low):
        viol += 1
ok(viol == 0, "合并后的相邻 K 线之间不能再存在包含关系", f"{viol} 处违反")

ok(all(b.confirmed_at == max(b.elements) for b in r.bars),
   "合并 K 线的确认时点必须等于其最后一根原始 K 线")
ok(all(r.bars[i].idx == i for i in range(len(r.bars))), "合并 K 线序号连续")

# ---------------------------------------------------------------- 分型
ok(len(r.fractals) > 4, "能识别出多个分型", str(len(r.fractals)))
bad_fx = [f for f in r.fractals
          if (f.kind == "top" and f.value != f.high) or (f.kind == "bottom" and f.value != f.low)]
ok(not bad_fx, "顶分型取值必须是 high、底分型必须是 low", str(len(bad_fx)))
ok(all(0 <= f.raw_idx < len(df) for f in r.fractals), "分型极值索引必须在数据范围内")
ok(all(f.confirmed_at >= f.raw_idx for f in r.fractals),
   "分型的确认时点不能早于极值所在那天（否则就是偷看未来）")

# ---------------------------------------------------------------- 笔
ok(len(r.bis) >= 3, "能识别出至少 3 笔", str(len(r.bis)))
alt = all(a.direction != b.direction for a, b in zip(r.bis, r.bis[1:]))
ok(alt, "相邻笔的方向必须交替")
ok(all(b.a.kind != b.b.kind for b in r.bis), "笔的两端必须是不同类型的端点")
ok(all(b.raw_end > b.raw_start for b in r.bis), "笔的终点索引必须大于起点")
bad_dir = [b for b in r.bis
           if (b.direction == "up" and not b.b.value > b.a.value)
           or (b.direction == "down" and not b.b.value < b.a.value)]
ok(not bad_dir, "向上笔终点必须更高、向下笔终点必须更低", str(len(bad_dir)))
ok(all(b.confirmed_at >= b.raw_end for b in r.bis),
   "笔的确认时点不能早于终点（否则等于用未完成的笔交易）")

# 最后一笔永远未封口：这是事实，不是缺陷
if r.bis:
    last = r.bis[-1]
    ok(last.confirmed_at >= last.raw_end, "最后一笔的确认时点仍然不早于它的终点")

# ---------------------------------------------------------------- 中枢
ok(all(z.ZG > z.ZD for z in r.zhongshus), "中枢上沿必须严格高于下沿")
ok(all(z.GG >= z.ZG and z.DD <= z.ZD for z in r.zhongshus),
   "中枢的 GG/DD 必须包住 ZG/ZD")
ok(all(z.bi_to >= z.bi_from + 2 for z in r.zhongshus),
   "中枢至少由三笔构成")
bad_end = [z for z in r.zhongshus
           if z.closed_at is not None and z.closed_at < z.confirmed_at]
ok(not bad_end, "中枢的结束时间不能早于它的成立时间", str(len(bad_end)))

# 中枢区间必须真的被成员笔覆盖
bad_cov = 0
for z in r.zhongshus:
    for b in r.bis[z.bi_from:z.bi_to + 1]:
        if b.high < z.ZD or b.low > z.ZG:
            bad_cov += 1
ok(bad_cov == 0, "中枢区间必须落在每一笔的波动范围内", f"{bad_cov} 处越界")

# ---------------------------------------------------------------- 信号
ok(len(r.signals) >= 1, "合成数据上应至少产生一个信号", str(len(r.signals)))
ok(all(0 <= s.confirmed_at < len(df) for s in r.signals), "信号确认时点必须在范围内")
ok(all(s.kind in CH.BUY_KINDS | CH.SELL_KINDS for s in r.signals), "信号类型必须在已知集合内")
# 信号所指的那笔必须已经存在，且确认时点不早于该笔终点
bad_sig = []
for s in r.signals:
    if not (0 <= s.bi_idx < len(r.bis)):
        bad_sig.append(s.kind)
        continue
    if s.confirmed_at < r.bis[s.bi_idx].raw_end:
        bad_sig.append(s.kind)
ok(not bad_sig, "信号确认时点不能早于它所指那笔的终点", str(bad_sig[:3]))

# 背驰信号的力度比必须 < 1（后段面积更小），否则不叫背驰
bb = [s for s in r.signals if s.ratio is not None]
ok(all(0 < s.ratio < CH.BEICHI_RATIO for s in bb),
   "背驰力度比必须落在 (0, 阈值) 区间内", str([s.ratio for s in bb][:4]))

# ---------------------------------------------------------------- 退化输入
ok(CH.analyze(pd.DataFrame()).bis == [], "空 DataFrame 不抛异常")
short = synth(n=30)
ok(CH.analyze(short).signals == [], "数据太短时不出信号（而不是硬算）")
flat = pd.DataFrame({
    "date": pd.bdate_range("2024-01-01", periods=200),
    "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
})
rf = CH.analyze(flat)
ok(rf.bis == [] and rf.signals == [], "完全没有波动时不出笔、不出信号")

# ---------------------------------------------------------------- 摘要
s = CH.summarize(df)
ok(isinstance(s, dict) and s.get("bis", 0) > 0, "summarize 返回结构化摘要")
ok("buy" in s and "sell" in s, "摘要必须同时给出买点与卖点统计")
if s.get("buy", {}).get("n"):
    ok(-30 < s["buy"]["mean"] < 30, "摘要里的收益必须是百分数量级",
       str(s["buy"]["mean"]))
ok(CH.summarize(pd.DataFrame()) == {}, "空输入时摘要为空对象而不是抛异常")

print()
print(f"{bad} 项未通过" if bad else "全部通过")
sys.exit(1 if bad else 0)
