"""数据源健康检查。

只对外暴露中文名称、状态和耗时——不给端点、不给参数。界面上的"数据源"入口读这里。

为什么要单独一层
    行情、资金、情绪来自不同的子域，限流与故障是各自独立的：
    push2 被限流时 datacenter 往往还通，护盘层因此还能出数。把它们的健康状态
    分开报，比笼统地说一句"数据加载失败"有用得多。

成本
    每次检测 11 个小请求（各取 1 条），并发执行约 1~2 秒。结果缓存 60 秒，
    面板打开才触发，不做后台轮询。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config as C
from . import datahub as D

_TTL = 60.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "items": []}


def _check(fn) -> tuple[bool, int, str]:
    t0 = time.time()
    try:
        ok, note = fn()
    except Exception as exc:  # noqa: BLE001
        ok, note = False, type(exc).__name__
    return bool(ok), int((time.time() - t0) * 1000), note


# ---------------------------------------------------------------------------
# 探针：每个只取 1 条，能通就算好
# ---------------------------------------------------------------------------
def _p_kline() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2HIS}/api/qt/stock/kline/get",
               {"secid": "1.000001", "klt": 101, "fqt": 1, "lmt": 3, "end": "20500101",
                "fields1": "f1,f2", "fields2": "f51,f53"})
    n = len(((j or {}).get("data") or {}).get("klines") or [])
    return n > 0, f"{n} 根"


def _p_spot() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2}/api/qt/clist/get",
               {"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": "m:0+t:6,m:1+t:2", "fields": "f12,f14"})
    return bool(((j or {}).get("data") or {}).get("diff")), "可读"


def _p_sector() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2}/api/qt/clist/get",
               {"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14"})
    return bool(((j or {}).get("data") or {}).get("diff")), "可读"


def _p_etf() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2}/api/qt/clist/get",
               {"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": "b:MK0021", "fields": "f12,f14"})
    return bool(((j or {}).get("data") or {}).get("diff")), "可读"


def _p_limit() -> tuple[bool, str]:
    day = dt.date.today()
    j = D._get(f"{C.PUSH2EX}/getTopicZTPool", {"ut": "7eea3edcaed734bea9cbfc24409ed989",
                                             "dpt": "wz.ztzt", "Pageindex": 0,
                                             "pagesize": 1, "sort": "fbt:asc",
                                             "date": day.strftime("%Y%m%d")})
    # 非交易日返回空池也算通（能连上就等于源可用）
    return j is not None, "可读"


def _p_news() -> tuple[bool, str]:
    j = D._get(f"{C.NP_LIST}/comm/web/getFastNewsList",
               {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
                "pageSize": 2, "req_trace": str(int(time.time() * 1000))}, timeout=8)
    n = len((((j or {}).get("data") or {}).get("fastNewsList")) or [])
    return n > 0, f"{n} 条"


def _p_overseas() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2}/api/qt/ulist.np/get",
               {"fltt": 2, "invt": 2, "fields": "f2,f3,f12,f14", "secids": "100.NDX,100.SPX"})
    return bool(((j or {}).get("data") or {}).get("diff")), "可读"


def _p_margin() -> tuple[bool, str]:
    j = D._get("https://datacenter-web.eastmoney.com/api/data/v1/get",
               {"reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL", "pageNumber": 1,
                "pageSize": 1, "sortColumns": "dim_date", "sortTypes": -1,
                "source": "WEB", "client": "WEB"}, timeout=12)
    n = len((((j or {}).get("result") or {}).get("data")) or [])
    return n > 0, "可读"


def _p_share() -> tuple[bool, str]:
    import re
    try:
        r = D._session().get("https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
                             params={"type": "gmbd", "code": "510300", "rt": "0.1"},
                             timeout=12,
                             headers={"Referer": "https://fundf10.eastmoney.com/gmbd_510300.html"})
        txt = r.text if r.status_code == 200 else ""
    except Exception:  # noqa: BLE001
        txt = ""
    return len(re.findall(r"<td", txt)) > 3, "可读"


def _p_tail() -> tuple[bool, str]:
    j = D._get(f"{C.PUSH2HIS}/api/qt/stock/kline/get",
               {"secid": "1.000001", "klt": 5, "fqt": 1, "lmt": 10, "end": "20500101",
                "fields1": "f1,f2", "fields2": "f51,f53"})
    n = len(((j or {}).get("data") or {}).get("klines") or [])
    return n > 0, f"{n} 根"


def _p_backup() -> tuple[bool, str]:
    """备用源：腾讯行情。主源限流时用它兜底，故单独报一条。"""
    import requests
    r = requests.get("https://qt.gtimg.cn/q=sh000001",
                     headers={"User-Agent": C.UA, "Referer": "https://gu.qq.com/"},
                     timeout=8)
    return r.status_code == 200 and "~" in r.text, "在线"


SOURCES = [
    ("日线行情", "行情", _p_kline),
    ("全市场快照", "行情", _p_spot),
    ("板块与成分", "行情", _p_sector),
    ("ETF 行情", "行情", _p_etf),
    ("涨跌停家数", "行情", _p_limit),
    ("隔夜外盘", "行情", _p_overseas),
    ("7×24 快讯", "情绪", _p_news),
    ("两融余额", "资金", _p_margin),
    ("ETF 份额", "资金", _p_share),
    ("尾盘分钟线", "资金", _p_tail),
    ("备用源（腾讯）", "备用", _p_backup),
]


def snapshot(force: bool = False) -> dict:
    """返回 {at, items:[{name, group, ok, ms, note}], ok, total, degraded}。"""
    with _lock:
        fresh = (time.time() - _cache["at"]) < _TTL
        if fresh and _cache["items"] and not force:
            items = _cache["items"]
        else:
            with ThreadPoolExecutor(max_workers=6) as ex:
                results = list(ex.map(lambda s: _check(s[2]), SOURCES))
            items = [{"name": s[0], "group": s[1], "ok": r[0], "ms": r[1], "note": r[2]}
                     for s, r in zip(SOURCES, results)]
            _cache["items"] = items
            _cache["at"] = time.time()
    good = sum(1 for x in items if x["ok"])
    return {
        "at": dt.datetime.fromtimestamp(_cache["at"]).strftime("%H:%M:%S"),
        "items": items,
        "ok": good,
        "total": len(items),
        "degraded": [x["name"] for x in items if not x["ok"]],
        # 本次构建有没有落到备用源。备用源能用，但缺成交额，属于"活着但残血"。
        "fallback": dict(getattr(D, "FALLBACK_USED", {})),
    }
