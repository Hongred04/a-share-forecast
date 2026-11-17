"""JSON 序列化的最后一道防线：绝不让 NaN / Infinity 写成非法字面量。

为什么需要这一层
    Python 的 json 默认 allow_nan=True，会把 float('nan') 写成裸的 `NaN`。
    这不是合法 JSON，浏览器的 JSON.parse 会直接抛
    `SyntaxError: Unexpected token 'N'`，整个响应作废——前端表现为
    "一直转圈 / 点刷新没反应"，而服务端日志一片正常。

    实测踩过一次：集合竞价时某个板块只有 1 只成分股通过过滤，
    robust_z 对单元素求 std() 得到 NaN，一路传到个股概率，
    state.json 里出现 `"prob": NaN`，整页数据刷不出来。
    根因已经在 robust_z 里修掉，但"源头修好"不等于"以后不会再漏"——
    任何新加的因子、新的 numpy 运算都可能再产出一个 NaN。
    所以序列化处必须有一次统一的兜底：把非有限浮点写成 null，
    让单个字段缺失，而不是让整份数据作废。

为什么是 null 而不是 0
    0 是一个有效的概率/收益值，会伪装成"算出来了，结果是零"。
    null 则明确表示"这个格没数"，前端 `pc(null)` 会显示占位符，
    含义不会被误读。
"""
from __future__ import annotations

import json
import math
from typing import Any


def clean(o: Any) -> Any:
    """递归把 NaN / ±Inf 换成 None，其余原样返回。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    # numpy 标量：np.float64 是 float 子类会被上面拦下，
    # np.float32 / np.int64 不是，单独判一次 item()
    item = getattr(o, "item", None)
    if callable(item) and getattr(o, "shape", None) == ():
        try:
            v = o.item()
        except Exception:  # noqa: BLE001
            return o
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        return v
    return o


def dumps(o: Any, **kw: Any) -> str:
    """等价 json.dumps，但保证输出是合法 JSON。"""
    kw.pop("allow_nan", None)
    return json.dumps(clean(o), ensure_ascii=False, allow_nan=False, **kw)
