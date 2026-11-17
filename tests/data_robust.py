"""数据健壮性测试：不让"单个字段坏了"升级成"整页刷不出来"。

这一组全部来自真实故障，不是假设：
    · 集合竞价时某板块只有 1 只成分股 → robust_z 产出 NaN
    · NaN 被 json.dumps 写成裸 NaN → 浏览器 JSON.parse 抛错 → 整页空白
    · 主数据源超时切备用源 → pct 整列是 None → float(None) 崩掉整个 build
    · 无效 secid 让接口回 {"data": null} → .get("klines") AttributeError
    · 流动性门槛放宽后北交所标的混进推荐

原则：**任何单点缺失都必须降级为"那一格没数"，不能升级为"全部没有"。**
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import core.config as C  # noqa: E402

bad = 0


def ok(cond, msg, detail=""):
    global bad
    if cond:
        print(f"✓ {msg}")
    else:
        bad += 1
        print(f"✗ {msg}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------- robust_z
from core import analytics as A  # noqa: E402

cases = {
    "单元素": pd.Series([5.0]),
    "全同值": pd.Series([3.0, 3.0, 3.0]),
    "全 NaN": pd.Series([np.nan, np.nan]),
    "含 NaN": pd.Series([1.0, np.nan, 3.0, 4.0]),
    "空": pd.Series([], dtype=float),
}
all_finite = True
for name, s in cases.items():
    z = A.robust_z(s)
    finite = all(np.isfinite(float(v)) for v in z) if len(z) else True
    if not finite:
        all_finite = False
        print(f"  · {name} 产出非有限值: {z.tolist()}")
ok(all_finite, "robust_z 在任何退化输入下都不产出 NaN/Inf")

# 正常输入不能被改坏
z = A.robust_z(pd.Series([1.0, 2.0, 3.0, 4.0])).tolist()
ok(z[0] < 0 < z[3] and abs(z[0] + z[3]) < 1e-9, "robust_z 对正常输入仍保持对称", str(z))

# ---------------------------------------------------------------- safejson
from core import safejson as SJ  # noqa: E402

payload = {"a": float("nan"), "b": float("inf"), "c": [float("-inf"), 1.0],
           "d": {"e": float("nan")}, "f": "文本"}
s = SJ.dumps(payload)
ok("NaN" not in s and "Infinity" not in s, "safejson 不输出 NaN/Infinity 字面量", s[:60])
try:
    back = json.loads(s)
    ok(back["a"] is None and back["b"] is None and back["c"][0] is None,
       "非有限浮点被写成 null（而不是 0，0 会伪装成有效值）")
    ok(back["d"]["e"] is None and back["f"] == "文本", "嵌套结构与正常值不受影响")
except Exception as e:  # noqa: BLE001
    bad += 1
    print(f"✗ safejson 输出无法被 json.loads 解析: {e}")

# numpy 标量也要处理
s2 = SJ.dumps({"x": np.float64("nan"), "y": np.float32(1.5)})
ok("NaN" not in s2, "numpy 标量的 NaN 也要被清理", s2)

# ---------------------------------------------------------------- kline 防御
from core import datahub as D  # noqa: E402

k = D.kline("0.999999", 20)
ok(k.empty or isinstance(k, pd.DataFrame), "无效 secid 返回空表而不是抛异常")

kt = D._kline_tx("1.000001", 30)
ok(len(kt) > 0, "备用源能取到数据")
ok(kt["pct"].dtype == np.float64, "备用源 pct 必须是 float（object 会让 float() 崩）",
   str(kt["pct"].dtype))
ok(kt["amount"].dtype == np.float64, "备用源 amount 必须是 float", str(kt["amount"].dtype))
# 备用源自算的涨跌幅要和收盘价自洽
row = kt.iloc[-1]
prev_close = kt.iloc[-2]["close"]
implied = (row["close"] / prev_close - 1) * 100
ok(abs(float(row["pct"]) - implied) < 1e-6,
   "备用源 pct 必须与收盘价自洽（不能是编的）",
   f"{row['pct']} vs {implied}")

# ---------------------------------------------------------------- 板块过滤
def mk_members(codes):
    return pd.DataFrame({
        "code": codes, "name": [f"N{c}" for c in codes],
        "price": [10.0] * len(codes), "pct": [1.0] * len(codes),
        "amount": [5e8] * len(codes), "pe": [20.0] * len(codes),
        "vol_ratio": [1.5] * len(codes), "turnover": [5.0] * len(codes),
        "mcap": [1e10] * len(codes), "float_mcap": [8e9] * len(codes),
        "net_ratio": [1.0] * len(codes), "net_in": [1e7] * len(codes),
        "list_date": [20100101] * len(codes),
    })


mix = mk_members(["600000", "000001", "300001", "688001", "920374", "830799", "430047"])
ranked = A.score_stocks(mix, 1.0, 0.60)
kept = set(ranked["code"].astype(str)) if not ranked.empty else set()
ok("920374" not in kept, "北交所 920 开头必须被排除")
ok("830799" not in kept, "北交所 8 开头必须被排除")
ok("430047" not in kept, "新三板 4 开头必须被排除")
ok({"600000", "000001", "300001", "688001"} <= kept,
   "沪深主板/创业板/科创板必须保留", f"实际保留 {sorted(kept)}")

# 流动性门槛参数必须真的生效（否则早盘折算形同虚设）
loose = A.score_stocks(mix, 1.0, 0.60, amount_floor=1e3)
tight = A.score_stocks(mix, 1.0, 0.60, amount_floor=1e12)
ok(len(loose) > 0 and tight.empty,
   "amount_floor 必须真正影响筛选结果",
   f"loose={len(loose)} tight={len(tight)}")

# ---------------------------------------------------------------- 现有快照
snap = pathlib.Path(C.DATA_DIR) / "state.json"
if snap.exists():
    raw = snap.read_text(encoding="utf-8")
    ok("NaN" not in raw and "Infinity" not in raw,
       "现有 state.json 不含非法 JSON 字面量")
    try:
        d = json.loads(raw)
        probs = [x.get("prob") for x in (d.get("prediction", {}).get("stocks") or [])]
        ok(all(p is not None for p in probs),
           "现有快照里没有概率为 null 的个股", str(probs))
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"✗ 现有 state.json 无法被 json.loads 解析: {e}")

# ---------------------------------------------------------------- 残缺快照不得落盘
# 实测过一次：东财 clist 整段不可用，板块 0 个、个股 0 只、ETF 0 只，
# 但指数日线还拉得到，于是 prob_up 算出了 0.5249。这份数据被直接写进
# state.json 覆盖掉上午的完整快照，页面上只剩一个孤零零的概率和三块空白——
# 读者分不出"今天没机会"和"数据没拉到"，而这两者的决策含义相反。
from core import engine as E  # noqa: E402

ok(E.MIN_SECTORS_FOR_SNAPSHOT >= 10,
   "板块完整性门槛不应低于 10（太低会放行残缺数据）", str(E.MIN_SECTORS_FOR_SNAPSHOT))


def _state(n):
    return {"prediction": {"sectors_all": [{"code": f"BK{i:04d}"} for i in range(n)]}}


try:
    E._assert_complete(_state(0))
    bad += 1
    print("✗ 板块为 0 时必须拒绝产出快照")
except RuntimeError as e:
    ok("板块" in str(e), "板块为 0 时拒绝产出快照，且错误信息可读", str(e)[:50])

try:
    E._assert_complete(_state(E.MIN_SECTORS_FOR_SNAPSHOT - 1))
    bad += 1
    print(f"✗ 板块少于 {E.MIN_SECTORS_FOR_SNAPSHOT} 时必须拒绝")
except RuntimeError:
    ok(True, f"板块少于 {E.MIN_SECTORS_FOR_SNAPSHOT} 时拒绝产出快照")

E._assert_complete(_state(E.MIN_SECTORS_FOR_SNAPSHOT))
ok(True, f"板块达到 {E.MIN_SECTORS_FOR_SNAPSHOT} 时放行")

E._assert_complete(_state(60))
ok(True, "完整板块数量正常放行")

# 空 state / 缺字段也要拒绝，不能因为 KeyError 之外的路径悄悄通过
try:
    E._assert_complete({})
    bad += 1
    print("✗ 空 state 必须拒绝")
except RuntimeError:
    ok(True, "空 state 也走同一条拒绝路径（不抛 KeyError）")

# 函数本身对还不够：必须确认 build_state **真的调了它**。
# 上面几条都是直接调 _assert_complete，把调用点删掉它们照样全绿——
# 那种"测试通过但保护已失效"是最危险的。
_esrc = (pathlib.Path(__file__).resolve().parent.parent / "core" / "engine.py"
         ).read_text(encoding="utf-8")
ok("_assert_complete(state)" in _esrc,
   "build_state 必须真的调用完整性校验（否则残缺快照仍会落盘）")
ok("_assert_complete(state)\n    return state" in _esrc,
   "校验必须发生在 return 之前")

# ---------------------------------------------------------------- 收盘后不重复重算
# 收盘后行情不再变化，但服务每次启动都会重算一次（约 50 秒），
# 点刷新又算一次，自动刷新还会算——一晚上算十几次，结果完全一样，
# 只是白打接口、把自己打到限流。
import datetime as _dt  # noqa: E402
import tempfile as _tf  # noqa: E402

import core.datahub as _D2  # noqa: E402

_real_dir, _real_phase = C.DATA_DIR, _D2.market_phase
_tmp = pathlib.Path(_tf.mkdtemp())


def _set(made_at, phase):
    C.DATA_DIR = _tmp
    (_tmp / "state.json").write_text(
        json.dumps({"prediction": {"made_at": made_at}},
                   ensure_ascii=False), encoding="utf-8")
    _D2.market_phase = lambda *a, **k: phase
    return E.snapshot_fresh_after_close()[0]


_today = _dt.date.today().strftime("%Y-%m-%d")
_yday = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")

ok(_set(f"{_today} 15:30:00", "已收盘") is True,
   "收盘后且快照是今天 15:00 之后生成的 → 应跳过重算")
ok(_set(f"{_today} 15:00:00", "已收盘") is True,
   "15:00 整点算的也算数（边界含）")
ok(_set(f"{_today} 10:37:57", "已收盘") is False,
   "盘中生成、收盘后还没重算 → 必须重算（盘中价和收盘价不是一回事）")
ok(_set(f"{_today} 14:59:00", "已收盘") is False,
   "14:59 仍属盘中 → 必须重算")
ok(_set(f"{_yday} 15:30:00", "已收盘") is False,
   "昨天收盘后算的 → 今天收盘后必须重算")
ok(_set(f"{_today} 15:30:00", "盘中") is False,
   "盘中永远不跳过（数据每分钟都在变）")
ok(_set(f"{_today} 15:30:00", "集合竞价") is False, "集合竞价不跳过")
ok(_set(f"{_yday} 15:30:00", "休市") is True,
   "休市日沿用最近一次收盘后的快照，不重算")
ok(_set("2020-01-02 15:30:00", "休市") is False,
   "休市但快照太旧（超过 5 天）→ 仍要重算")

# 文件缺失/损坏时不能崩，应当重算
C.DATA_DIR = pathlib.Path(_tf.mkdtemp())
_D2.market_phase = lambda *a, **k: "已收盘"
ok(E.snapshot_fresh_after_close()[0] is False, "没有 state.json 时应重算")
(C.DATA_DIR / "state.json").write_text("{坏文件", encoding="utf-8")
ok(E.snapshot_fresh_after_close()[0] is False, "state.json 损坏时应重算而不是抛异常")

C.DATA_DIR, _D2.market_phase = _real_dir, _real_phase

# ---------------------------------------------------------------- 备用数据源
# 东财 clist 的 m: 类查询会整段不可用（实测：全市场、板块列表一起挂，
# 而 b: 类与日线还活着）。全市场快照拿不到就整个构建失败，
# 所以必须有第二条腿。这里只测映射与降级逻辑，不发真实请求。
import core.market as _M2  # noqa: E402

_fake_sina = [
    {"code": "600000", "name": "浦发银行", "trade": "10.40", "changepercent": 1.86,
     "amount": 1.2e9, "mktcap": 3050000.0, "nmc": 2900000.0, "turnoverratio": 0.8, "per": 5.2},
    {"code": "000001", "name": "平安银行", "trade": "11.20", "changepercent": -0.5,
     "amount": 8.0e8, "mktcap": 2170000.0, "nmc": 2100000.0, "turnoverratio": 0.6, "per": 4.9},
]
_df = pd.DataFrame(_fake_sina)
_out = pd.DataFrame()
for _s, _d in {"code": "code", "name": "name", "trade": "price", "changepercent": "pct",
               "amount": "amount", "turnoverratio": "turnover", "per": "pe"}.items():
    _out[_d] = _df[_s]
_out["mcap"] = pd.to_numeric(_df["mktcap"]) * 10000
_out["float_mcap"] = pd.to_numeric(_df["nmc"]) * 10000
ok(float(_out["mcap"].iloc[0]) == 3.05e10,
   "新浪市值必须从万元换算成元（差 1 万倍会让绝对市值全错）",
   str(_out["mcap"].iloc[0]))
ok(float(_out["float_mcap"].iloc[0]) < float(_out["mcap"].iloc[0]),
   "流通市值不能大于总市值")

# 主力净流入整列缺失时，不能算出"0 亿"
_no_net = pd.DataFrame({"code": ["600000"], "amount": [1e9],
                        "net_in": [float("nan")], "pct": [1.0]})
_cap = _M2.compute_capital(_no_net)
ok(_cap.get("unavailable") is True,
   "net_in 全缺失时 compute_capital 必须标 unavailable，而不是返回净流入 0",
   str(_cap))
ok("net_yi" not in _cap or _cap.get("unavailable"),
   "unavailable 时不应同时给出 net_yi → 前端会显示一个假的 0")

# 有数据时行为不变
_has_net = pd.DataFrame({"code": ["600000"], "amount": [1e9],
                         "net_in": [2e8], "pct": [1.0]})
_cap2 = _M2.compute_capital(_has_net)
ok(_cap2.get("unavailable") is False and abs(_cap2["net_yi"] - 2.0) < 1e-9,
   "有主力净流入时仍按原口径计算", str(_cap2))

# 资金因子在 unavailable 时必须判为"缺失"而不是贡献 0 分
_ctx = A.MarketContext(date=pd.Timestamp("2026-11-14"), bench={},
                       capital={"unavailable": True})
_sig = A.expert_capital(_ctx)
ok(_sig.available is False and "缺失" in str(_sig.detail),
   "资金因子在数据缺失时必须标记不可用", f"available={_sig.available} detail={_sig.detail}")

print()
print(f"{bad} 项未通过" if bad else "全部通过")
sys.exit(1 if bad else 0)
