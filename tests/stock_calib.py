"""个股概率校准层的行为测试。

这一层最容易出的不是崩溃，而是"看起来很对"：
    · 样本不够却报了个校准后的概率
    · 界面显示 43/40 已达标，拟合实际用了 0 条
    · 拿校准输出再拟合，几轮后概率被压平而 Brier 反而变好
所以断言的重点全部放在"数字的口径是否一致"，而不是"函数能不能跑通"。
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import core.config as C  # noqa: E402

bad = 0


def ok(cond, msg, detail=""):
    global bad
    if cond:
        print(f"✓ {msg}")
    else:
        bad += 1
        print(f"✗ {msg}" + (f"  [{detail}]" if detail else ""))


# ---------------------------------------------------------------- 恒等性
from core import analytics as A  # noqa: E402

raw = [0.45, 0.55, 0.65, 0.70]
q, ready = A.apply_stock_calib(raw, None)
ok(not ready, "无样本时 ready 必须为 False")
ok(all(abs(a - b) < 1e-12 for a, b in zip(q, raw)),
   "无样本时必须原样返回 raw（不能偷偷收缩）", f"{list(q)}")

q2, ready2 = A.apply_stock_calib(raw, {"n": A.STOCK_CALIB_MIN_N - 1, "lam": 0.5})
ok(not ready2, f"n={A.STOCK_CALIB_MIN_N - 1} 时仍未就绪（门槛是 >=", str(A.STOCK_CALIB_MIN_N))
# 未就绪时，即使传了 lam 也必须保持恒等——参数没被采用，就不能悄悄生效，
# 否则"未校准"的标签和实际的数字会互相打架。
ok(all(abs(a - b) < 1e-12 for a, b in zip(q2, raw)),
   "未就绪时即便传了 lam 也不得收缩（否则标注与现实不符）", f"{list(q2)}")

q3, ready3 = A.apply_stock_calib(raw, {"n": A.STOCK_CALIB_MIN_N, "lam": 0.5,
                                       "base": 0.50})
ok(ready3, f"n={A.STOCK_CALIB_MIN_N} 时必须就绪")
ok(q3[2] < q2[2], "就绪后应真的发生收缩", f"{q2[2]:.3f} -> {q3[2]:.3f}")

# 极端输入不能溢出
ql, _ = A.apply_stock_calib([0.0, 1.0, -5.0, 9.0], {"n": 100, "lam": 0.5, "slope": 1.4})
ok(all(0.0 <= float(x) <= 1.0 for x in ql), "极端输入的概率必须仍落在 [0,1]", f"{list(ql)}")

# ---------------------------------------------------------------- 拟合器
def gen(n, hit, lo, hi, seed):
    random.seed(seed)
    return [{"prob_raw": random.uniform(lo, hi),
             "actual_pct": (1.0 if random.random() < hit else -1.0)} for _ in range(n)]


# 过度自信：报 62~72%，实际只中 48%
f = A.fit_stock_calib(gen(120, 0.48, 0.62, 0.72, 11))
ok(f is not None, "样本充足时应给出拟合结果")
ok(f["improved"], "过度自信样本应被判为有改善", str(f))
ok(abs(f["base"] - f["hit_rate"]) < 1e-9,
   "base 必须等于实测命中率（否则 read 的人无法解释这个数）",
   f"base={f['base']} hit_rate={f['hit_rate']}")
ok(f["lam"] < 0.9, "过度自信时 lam 应明显小于 1", f"lam={f['lam']}")
ok(f["brier"] < f["brier_raw"], "拟合后 Brier 必须低于未校准",
   f"{f['brier']} vs {f['brier_raw']}")

# 有真实信号：raw 越高越容易涨，应保留区分度
random.seed(5)
sig = []
for _ in range(200):
    r = random.uniform(0.50, 0.80)
    pr = 0.30 + 0.5 * (r - 0.50) / 0.30
    sig.append({"prob_raw": r, "actual_pct": (1.0 if random.random() < pr else -1.0)})
f2 = A.fit_stock_calib(sig)
ok(not f2["weak"], "存在真实信号时不应判为 weak", f"spread={f2['spread']}")
qs, _ = A.apply_stock_calib([0.50, 0.65, 0.80], f2)
ok(qs[2] - qs[0] > 0.05, "有信号时校准后仍应保持区分度",
   f"极差 {qs[2] - qs[0]:.3f}")

# 纯噪声：raw 与结果无关，必须承认分不开
fn = A.fit_stock_calib(gen(120, 0.48, 0.62, 0.72, 11))
ok(fn["weak"], "取不到信号时必须标 weak（不能假装有区分度）", f"lam={fn['lam']}")

# 样本不足直接拒绝
ok(A.fit_stock_calib(gen(10, 0.5, 0.6, 0.7, 3)) is None,
   f"样本 < {A.STOCK_CALIB_MIN_N} 时必须返回 None")

# ---------------------------------------------------------------- 落库与口径
tmp = pathlib.Path(tempfile.mkdtemp()) / "cal.db"
shutil.copy(pathlib.Path(C.DB_PATH), tmp) if pathlib.Path(C.DB_PATH).exists() else None
C.DB_PATH = tmp
import importlib  # noqa: E402
from core import store as S  # noqa: E402

importlib.reload(S)
S.DB_PATH = tmp
S.init_db()

# 清空，造两种行：带 prob_raw 的（可拟合）和不带的（老口径，必须排除）
with S.db() as c:
    c.execute("DELETE FROM stock_pred")
    for i in range(6):
        c.execute("INSERT INTO stock_pred(target,code,prob,prob_raw,actual_pct) "
                  "VALUES(?,?,?,?,?)", ("2026-01-01", f"9{i:05d}", 0.65, 0.65, 1.0))
    for i in range(4):
        c.execute("INSERT INTO stock_pred(target,code,prob,actual_pct) "
                  "VALUES(?,?,?,?)", ("2025-12-01", f"8{i:05d}", 0.42, -1.0))

ok(S.stock_sample_count() == 6,
   "样本计数只能算带 prob_raw 的行，老口径不能混进来",
   f"实际 {S.stock_sample_count()}，期望 6（库里有 10 行已评价）")

# 计数口径必须与拟合取数口径一致
with S.db() as c:
    fit_rows = c.execute("SELECT COUNT(*) FROM stock_pred "
                         "WHERE actual_pct IS NOT NULL AND prob_raw IS NOT NULL").fetchone()[0]
ok(int(fit_rows) == S.stock_sample_count(),
   "fit 的取数条件与 stock_sample_count 必须完全一致",
   f"fit={fit_rows} count={S.stock_sample_count()}")

# 默认参数：未校准时 lam 必须为 1.0（恒等），不能是凭空的收缩值
d = S.stock_calib()
ok(abs(float(d["lam"]) - 1.0) < 1e-9,
   "默认 lam 必须是 1.0（恒等），否则等于凭空假设模型过度自信", f"lam={d['lam']}")

# ---------------------------------------------------------------- 回归：kline 门槛
# review 曾用 limit=12 取K线，而 kline() 内部要求 >=20 根，导致个股评价
# 永远拿到空表、与实际数据无关地全部失败。
#
# 这里不能真的去请求网络：单元测试一旦依赖外部接口，网络一抖就红，
# 最后没人再信这个红灯（本地通过、打包版失败，正是这么发现的）。
# 改为静态校验所有调用方传的 limit 都不低于 kline 的内部门槛。
import re  # noqa: E402

LIMIT_FLOOR = 20   # 与 datahub.kline 里 `if len(out) >= 20` 保持一致

_root = pathlib.Path(__file__).resolve().parent.parent
_calls = []
for _py in (_root / "core").glob("*.py"):
    for _line in _py.read_text(encoding="utf-8").splitlines():
        if "kline(" not in _line or _line.lstrip().startswith("#"):
            continue
        # 只校验显式传了数字的调用；不传的走默认 LOOKBACK_DAYS（1300），安全。
        # 不用正则匹配整个调用：secid 参数本身带括号（_stock_secid(r["code"])），
        # 写成 kline\(\s*[^,()]+ 会一个都匹配不到——测试静默空转是最坏的一种。
        _m = re.search(r",\s*(\d+)\s*\)", _line)
        if _m:
            _calls.append((_py.name, int(_m.group(1))))

ok(bool(_calls), "应当能在核心代码里找到显式传 limit 的 kline 调用", str(_calls))
_too_small = [(f, n) for f, n in _calls if n < LIMIT_FLOOR]
ok(not _too_small,
   f"所有 kline 调用的 limit 都必须 >= {LIMIT_FLOOR}，否则拿到空表",
   str(_too_small))

_src = (_root / "core" / "datahub.py").read_text(encoding="utf-8")
ok("len(out) >= 20" in _src,
   "kline 的内部门槛若改变，本测试的 LIMIT_FLOOR 必须同步修改")

print()
print(f"{bad} 项未通过" if bad else "全部通过")
sys.exit(1 if bad else 0)
