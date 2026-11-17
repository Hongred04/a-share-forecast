"""数据层：直连东方财富公开行情端点（与 AKShare / efinance 同源），带重试、并发与磁盘缓存。

设计原则
- 不依赖 akshare/efinance 等第三方库，避免版本漂移导致接口失效；
- 任何单一数据源失败都不阻断主流程，返回空表并降级；
- 盘中与盘后同一套接口，K 线最后一根即当日实时/收盘数据。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd
import requests

from . import config as C
from . import safejson as SJ

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(C.HEADERS)
        _local.s = s
    return s


def _get(url: str, params: dict[str, Any] | None = None, timeout: int = C.TIMEOUT,
         retries: int = C.RETRIES) -> dict | None:
    last = None
    for i in range(retries + 1):
        try:
            r = _session().get(url, params=params, timeout=timeout)
            if r.status_code == 200 and r.text.strip():
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(0.35 * (i + 1))
    _ = last
    return None


def _get_list(url: str, params: dict[str, Any], timeout: int = C.TIMEOUT,
              tries: int = 3) -> dict | None:
    """请求一个"本该有内容"的列表接口，空列表视为失败并重试。

    东财 clist 接口在连发/并发时会返回 **HTTP 200 但 diff 为空**。
    _get 只看状态码和响应体是否为空串，于是判定成功，上层拿到空表后
    静默降级——表现为"9 只个股只出了 3 只""ETF 面板整块是空的"，
    而日志里一片正常，查不出问题。实测连拉 5 个板块，首轮 3 个返回空，
    隔一秒再拉全部正常，说明这是限流而不是数据真的没有。

    只用于"必然有条目"的接口（板块成分、ETF 列表）。真正的空结果
    （例如某只票没有 K 线）不该走这里，否则会白白重试三次。
    """
    for i in range(tries):
        r = _get(url, params, timeout=timeout)
        diff = (r or {}).get("data") or {}
        if diff.get("diff"):
            return r
        time.sleep(0.45 * (i + 1))
    return None


# --------------------------------------------------------------------------------------
# 交易日历
# --------------------------------------------------------------------------------------
# 为什么要写死而不是算
#     A 股的休市日跟着农历走（春节、中秋），没有可以推的公式，只能查公布的安排。
#     但这张表会**过期**：只写到 2026 时，2027-01-01 会被当成交易日，target 算错、
#     回放样本错位，而且全程不报错——这是最坏的一种坏。
#
# 三层防线
#     1. 硬编码表覆盖到 2027（每年国务院公布后补一行）；
#     2. 超出覆盖范围时**不再假装知道**：`calendar_coverage()` 会把情况报出来，
#        界面据此提示"交易日历已过期，请更新"，而不是静默按工作日处理；
#     3. 有指数日线时，`sync_from_kline()` 用真实交易日反推缺失的休市日，
#        覆盖表的缺口——数据比日历可靠。
#
# 注意"调休上班日"：2027-01-31 是周日但调休上班，A 股**仍然不开市**。
# 所以不能简单地"周末休、工作日开"，表里的休市日永远优先。

HOLIDAYS = {
    # ---- 2026 ----
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    # ---- 2027（据国办 2026-11-05 通知；只列落在工作日、真正影响开市的）----
    # 元旦 1/1~1/3 → 工作日只有 1/1
    "2027-01-01",
    # 春节 2/5~2/13 → 工作日 2/5、2/8~2/12（1/31 与 2/14 是调休上班，A股不开市）
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    # 清明 4/3~4/5 → 工作日只有 4/5
    "2027-04-05",
    # 劳动节 5/1~5/5 → 工作日 5/3~5/5（5/8 调休上班，A股不开市）
    "2027-05-03", "2027-05-04", "2027-05-05",
    # 端午 6/9、中秋 9/15
    "2027-06-09", "2027-09-15",
    # 国庆 10/1~10/7 → 工作日 10/1、10/4~10/7（9/26 与 10/9 调休上班，A股不开市）
    "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
}

# 兼容旧名（外部脚本/研究代码可能引用）
HOLIDAYS_2026 = HOLIDAYS

# 从日线反推出来的休市日（比如 2028 年还没公布时的空档）
_derived: set[str] = set()

# 硬编码表覆盖的最后一年
_COVERED_THROUGH = max(d[:4] for d in HOLIDAYS)


def calendar_coverage(today: dt.date | None = None) -> dict:
    """日历够不够用。到期前 90 天就开始提醒，别等到算错才发现。"""
    today = today or dt.date.today()
    year = today.year
    covered = year <= int(_COVERED_THROUGH)
    # 距离年底还剩多少天——年底才提醒就太晚了，元旦假期就在眼前
    left = (dt.date(year, 12, 31) - today).days
    stale = (not covered) or (year == int(_COVERED_THROUGH) and left <= 90)
    return {"covered": covered, "year": year, "covered_through": _COVERED_THROUGH,
            "days_to_year_end": left, "stale": stale,
            "msg": "" if not stale else
                   f"交易日历只写到了 {_COVERED_THROUGH} 年底，请补上新一年度的休市安排"}


def sync_from_kline(days) -> int:
    """用真实交易日反推休市日，补硬编码表的缺口。

    days 是任何一条主流指数的 date 序列。落在"工作日但没有日线"的日期就是休市，
    这是最可靠的真相——比任何日历表都准。只补未来 400 天内的，避免把早年缺数据的
    日期当成休市日灌进表里。
    """
    if days is None:
        return 0
    try:
        have = {str(x)[:10] for x in days}
    except Exception:  # noqa: BLE001
        return 0
    if len(have) < 20:
        return 0
    today = dt.date.today()
    # 只补"区间内部"的缺口。
    # 边缘的缺口不能碰：数据可能只是被截断（网络少拉几天、lmt 不够），
    # 把 11-06 当成休市会导致 target 直接错一天——比日历过期更糟。
    try:
        lo = max(dt.date.fromisoformat(min(have)), today - dt.timedelta(days=120))
        hi = min(dt.date.fromisoformat(max(have)), today - dt.timedelta(days=1))
    except Exception:  # noqa: BLE001
        return 0
    d = lo
    added = 0
    while d <= hi:
        s = d.isoformat()
        if d.weekday() < 5 and s not in have and s not in HOLIDAYS and s not in _derived:
            _derived.add(s)
            added += 1
        d += dt.timedelta(days=1)
    return added


def _is_off(day: dt.date) -> bool:
    s = day.isoformat()
    return s in HOLIDAYS or s in _derived


def next_trading_day(day: dt.date) -> dt.date:
    d = day + dt.timedelta(days=1)
    for _ in range(30):          # 春节连休最多 9 天 + 周末，30 天足够
        if d.weekday() < 5 and not _is_off(d):
            return d
        d += dt.timedelta(days=1)
    return d


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and not _is_off(day)


def market_phase(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    if not is_trading_day(now.date()):
        return "休市"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 15:
        return "盘前"
    if 9 * 60 + 15 <= hm < 9 * 60 + 30:
        return "集合竞价"
    if 9 * 60 + 30 <= hm < 11 * 60 + 30:
        return "盘中"
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "午间休市"
    if 13 * 60 <= hm < 15 * 60:
        return "盘中"
    return "已收盘"


# --------------------------------------------------------------------------------------
# K 线
# --------------------------------------------------------------------------------------
_KLINE_COLS = ["date", "open", "close", "high", "low",
               "volume", "amount", "amplitude", "pct", "chg", "turnover"]


# 备用源被启用过就记一笔，界面的"数据源"面板据此提示"已在用备用源"
FALLBACK_USED = {"kline": False, "sector_members": [], "etf": False,
                 "stock_spot": None}

_TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tx_symbol(secid: str) -> str | None:
    """1.000001 → sh000001；0.399001 → sz399001。海外与板块代码没有对应，返回 None。"""
    market, _, code = str(secid).partition(".")
    if market == "1":
        return "sh" + code
    if market == "0":
        return "sz" + code
    return None


def _kline_tx(secid: str, limit: int) -> pd.DataFrame:
    """备用源：腾讯日线。字段对齐东财，但**没有成交额**——amount 置空，
    量能结构信号会因此不可用（宁可少一个信号，也不编一个数字）。"""
    sym = _tx_symbol(secid)
    if not sym:
        return pd.DataFrame(columns=_KLINE_COLS)
    j = _get(_TX_KLINE, {"param": f"{sym},day,,,{max(int(limit), 20)},qfq"}, timeout=12,
             retries=1)
    arr = (((j or {}).get("data") or {}).get(sym) or {}).get("day") or []
    if not arr:
        return pd.DataFrame(columns=_KLINE_COLS)
    recs = []
    for row in arr:
        if len(row) < 6:
            continue
        d, o, c_, h, l, v = row[:6]
        recs.append([d, o, c_, h, l, v])
    df = pd.DataFrame(recs, columns=["date", "open", "close", "high", "low", "volume"])
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).reset_index(drop=True)

    # 备用源不直接给涨跌幅，但给了收盘价——自己算，不能留空。
    # 曾经这里是 `recs.append([..., None, None, None, None, None])`，
    # 把 pct 也留成 None，于是整列变成 object dtype，全为 None。
    # 主源一旦超时切到备用源，bench_raw["pct"] 就整列是 None，
    # float(cur["pct"]) 在复盘层直接抛 TypeError，build 整个失败——
    # 表现就是"点刷新没反应、快照一直不更新"。amount 当时被显式置了 NaN，
    # 说明写的时候已经想到 object dtype 会炸 rolling，只是漏了 pct。
    df["pct"] = df["close"].pct_change() * 100
    df["chg"] = df["close"].diff()
    # 成交额与换手率是真的没有：显式置 NaN（不是 None），保持 float dtype。
    df["amount"] = float("nan")
    df["amplitude"] = float("nan")
    df["turnover"] = float("nan")
    return df[_KLINE_COLS].reset_index(drop=True)


def kline(secid: str, limit: int = C.LOOKBACK_DAYS, klt: int = 101) -> pd.DataFrame:
    """日线（前复权）。最后一根为当日实时行情（盘中）。

    东财失败时退到腾讯（仅日线、仅 A 股代码）。备用源缺成交额，
    属于"活着但残血"，界面会明说。
    """
    data = _get(
        f"{C.PUSH2HIS}/api/qt/stock/kline/get",
        {
            "secid": secid, "klt": klt, "fqt": 1, "lmt": limit, "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        timeout=25,
    )
    # 注意是 .get("data") or {}，不能写成 .get("data", {})：
    # 接口对不存在的 secid 会返回 {"rc":0,"data":null}，键是存在的、值是 null，
    # 默认值 {} 根本不生效，随后 .get("klines") 就抛 AttributeError。
    # 备用源那行早就用了 `or {}`，主源这里漏了——北交所代码一进来就炸。
    rows = ((data or {}).get("data") or {}).get("klines") or []
    if rows:
        recs = [r.split(",") for r in rows]
        df = pd.DataFrame(recs, columns=_KLINE_COLS)
        for c in _KLINE_COLS[1:]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        out = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(out) >= 20:
            return out
    if klt != 101:
        return pd.DataFrame(columns=_KLINE_COLS)
    alt = _kline_tx(secid, limit)
    if not alt.empty:
        FALLBACK_USED["kline"] = True
        return alt
    return pd.DataFrame(columns=_KLINE_COLS)


def index_klines(limit: int = C.LOOKBACK_DAYS) -> dict[str, pd.DataFrame]:
    return {name: kline(secid, limit) for name, secid in C.INDEXES.items()}


def overseas_klines(limit: int = C.LOOKBACK_DAYS) -> dict[str, pd.DataFrame]:
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {n: ex.submit(kline, s, limit) for n, s in C.US_INDEXES.items()}
        return {n: f.result() for n, f in futs.items()}


def overseas_leaders() -> pd.DataFrame:
    """美股科技龙头隔夜表现（含盘后，方向即财报/指引的市场定价）。"""
    secids = ",".join(C.US_LEADERS.values())
    data = _get(f"{C.PUSH2}/api/qt/ulist.np/get",
                {"fltt": 2, "secids": secids, "fields": "f2,f3,f4,f12,f14"})
    diff = (data or {}).get("data", {}).get("diff") or []
    if not diff:
        return pd.DataFrame(columns=["name", "code", "pct"])
    df = pd.DataFrame(diff).rename(columns={"f14": "name", "f12": "code", "f3": "pct"})
    df["pct"] = pd.to_numeric(df["pct"], errors="coerce")
    return df[["name", "code", "pct"]].dropna()


# --------------------------------------------------------------------------------------
# 板块
# --------------------------------------------------------------------------------------
_SECTOR_FIELDS = ("f2,f3,f6,f8,f12,f14,f20,f62,f104,f105,f128,f136,f140,f184")


def sector_list(kind: str = "industry") -> pd.DataFrame:
    fs = "m:90+t:2" if kind == "industry" else "m:90+t:3"
    out: list[dict] = []
    page = 1
    while page <= 8:
        # 第一页用"必须非空"的重试；后面几页空返回是真到头了，不再纠缠。
        # 板块列表是核心数据，首屏拿不到就等于当天白算——
        # 之前用普通 _get 时，限流返回 200 + 空 diff 会被当成"没有板块"，
        # 一路走到 build 才算失败，用户只看到一句"行情源可能限流"。
        if page == 1:
            data = _get_list(f"{C.PUSH2}/api/qt/clist/get", {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": fs, "fields": _SECTOR_FIELDS,
            })
        else:
            data = _get(f"{C.PUSH2}/api/qt/clist/get", {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": fs, "fields": _SECTOR_FIELDS,
            })
        d = (data or {}).get("data")
        if not d or not d.get("diff"):
            break
        out.extend(d["diff"])
        if len(out) >= int(d.get("total") or 0):
            break
        page += 1
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f6": "amount",
        "f8": "turnover", "f20": "mcap", "f62": "net_in", "f184": "net_ratio",
        "f104": "up_cnt", "f105": "down_cnt", "f128": "leader", "f136": "leader_pct",
        "f140": "leader_code",
    })
    for c in ["price", "pct", "amount", "turnover", "mcap", "net_in", "net_ratio",
              "up_cnt", "down_cnt", "leader_pct"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["kind"] = kind
    return df


def sector_klines(codes: list[str], limit: int = C.LOOKBACK_DAYS,
                  workers: int = 8) -> dict[str, pd.DataFrame]:
    def _one(code: str) -> tuple[str, pd.DataFrame]:
        return code, kline(f"90.{code}", limit)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(_one, codes))


def sector_members(code: str, limit: int = 400) -> pd.DataFrame:
    """板块成分股实时快照。

    空结果必须当成"这次没拿到"而不是"这个板块没有成分股"来对待。
    东财 clist 接口在连续请求时会返回 **HTTP 200 但 diff 为空**，
    _get 只看状态码和响应体非空，于是判定成功、不会重试。
    实测：连续拉 5 个板块，首轮有 3 个返回 0 只，隔一秒再拉全部正常。
    如果不在这里补一次重试，上层会静默跳过该板块——
    表现为"个股只出了 3 只"而界面一片正常，看不出是数据没拉到。
    """
    diff: list = []
    data = _get_list(f"{C.PUSH2}/api/qt/clist/get", {
        "pn": 1, "pz": limit, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3", "fs": f"b:{code}",
        "fields": "f2,f3,f6,f8,f9,f10,f12,f14,f20,f21,f26,f62,f184",
    })
    diff = ((data or {}).get("data") or {}).get("diff") or []
    if not diff:
        if code not in FALLBACK_USED["sector_members"]:
            FALLBACK_USED["sector_members"].append(code)
        return pd.DataFrame()
    df = pd.DataFrame(diff).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f6": "amount",
        "f8": "turnover", "f9": "pe", "f10": "vol_ratio", "f20": "mcap",
        "f21": "float_mcap", "f26": "list_date", "f62": "net_in", "f184": "net_ratio",
    })
    for c in df.columns:
        if c not in ("code", "name"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# --------------------------------------------------------------------------------------
# 全市场快照
# --------------------------------------------------------------------------------------
def _stock_spot_em(workers: int = 8) -> pd.DataFrame:
    """沪深京 A 股全市场快照（东财主源，约 5500 只），分页并发抓取。"""
    base = {
        "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f5,f6,f8,f9,f10,f12,f14,f20,f21,f26,f62,f100,f184",
    }
    first = _get(f"{C.PUSH2}/api/qt/clist/get", {**base, "pn": 1, "pz": 100})
    d = (first or {}).get("data") or {}
    total = int(d.get("total") or 0)
    recs: list[dict] = list(d.get("diff") or [])
    pages = min((total + 99) // 100, 70)
    if pages > 1:
        def _pg(p: int) -> list[dict]:
            r = _get(f"{C.PUSH2}/api/qt/clist/get", {**base, "pn": p, "pz": 100})
            return ((r or {}).get("data") or {}).get("diff") or []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk in ex.map(_pg, range(2, pages + 1)):
                recs.extend(chunk)
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f5": "volume",
        "f6": "amount", "f8": "turnover", "f9": "pe", "f10": "vol_ratio",
        "f20": "mcap", "f21": "float_mcap", "f26": "list_date", "f62": "net_in",
        "f100": "industry", "f184": "net_ratio",
    })
    for c in df.columns:
        if c not in ("code", "name", "industry"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    return df


def _stock_spot_sina(workers: int = 6, max_pages: int = 60) -> pd.DataFrame:
    """全市场快照的新浪备用源。

    为什么选新浪做这个备用
        东财的 `m:` 类查询（全市场、板块列表）会整段不可用，而 `b:` 类和日线
        还活着——说明是特定查询被限，不是网络断了。这时候如果全市场快照拿不到，
        整个构建就没法做（个股、板块上涨家数、宽度指标全依赖它）。
        新浪这个接口走的是另一套后端，实测同期可用，是真正意义上的"另一条腿"。

    字段差异（必须显式处理，否则会静默算错）
        · mktcap / nmc 单位是**万元**，东财是元 → 必须 ×10000。
          不换算的话市值因子会小四个数量级，稳健标准化后虽然还能排序，
          但"市值中位数"之类的绝对数值会全部错掉。
        · 没有量比、主力净流入、上市日期、所属行业 → 留 NaN / 空串。
          robust_z 会把 NaN 压成 0，这些因子等于不参与，不会污染结果；
          但必须在 degraded 里说清楚，否则读者不知道这一天的个股排名少了两个维度。
        · num 参数上限 100，翻页只能一页页来。
    """
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData")
    hdr = {"User-Agent": C.HEADERS.get("User-Agent", "Mozilla/5.0"),
           "Referer": "https://finance.sina.com.cn"}

    def _page(p: int) -> list[dict]:
        try:
            r = _session().get(url, params={"page": p, "num": 100, "sort": "symbol",
                                            "asc": 1, "node": "hs_a"},
                               headers=hdr, timeout=C.TIMEOUT)
            if r.status_code != 200 or not r.text.strip():
                return []
            j = r.json()
            return j if isinstance(j, list) else []
        except Exception:  # noqa: BLE001
            return []

    first = _page(1)
    if not first:
        return pd.DataFrame()
    recs = list(first)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(_page, range(2, max_pages + 1)):
            recs.extend(chunk)
    if len(recs) < 1000:          # 不到 1000 只说明翻页大面积失败，不如不用
        return pd.DataFrame()

    df = pd.DataFrame(recs)
    keep = {"code": "code", "name": "name", "trade": "price", "changepercent": "pct",
            "volume": "volume", "amount": "amount", "turnoverratio": "turnover",
            "per": "pe"}
    out = pd.DataFrame()
    for src, dst in keep.items():
        out[dst] = df[src] if src in df.columns else float("nan")
    # 万元 → 元
    out["mcap"] = pd.to_numeric(df.get("mktcap"), errors="coerce") * 10000
    out["float_mcap"] = pd.to_numeric(df.get("nmc"), errors="coerce") * 10000
    # 新浪不提供的字段：留 NaN，由 robust_z 压成 0（不参与），并在 degraded 里说明
    out["vol_ratio"] = float("nan")
    out["net_in"] = float("nan")
    out["net_ratio"] = float("nan")
    out["list_date"] = float("nan")
    out["industry"] = ""

    for c in ("price", "pct", "volume", "amount", "turnover", "pe"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out[pd.to_numeric(out["price"], errors="coerce") > 0]
    out = out.drop_duplicates(subset=["code"]).reset_index(drop=True)
    return out


def stock_spot(workers: int = 8) -> pd.DataFrame:
    """全市场快照。东财优先，空/残缺时退到新浪。

    正常全市场约 5500 只；少于 1000 只一律视为失败而不是"今天只有这么多股票"。
    两个源都在的时候用东财（字段更全：有行业、量比、主力净流入）。
    """
    df = _stock_spot_em(workers)
    if len(df) >= 1000:
        return df
    alt = _stock_spot_sina(workers)
    if len(alt) > len(df):
        FALLBACK_USED["stock_spot"] = "sina"
        return alt
    return df


# --------------------------------------------------------------------------------------
# 情绪：涨停/跌停
# --------------------------------------------------------------------------------------
def _pool(url: str, day: dt.date) -> int:
    data = _get(url, {
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 1, "sort": "fbt:asc",
        "date": day.strftime("%Y%m%d"),
    }, timeout=15)
    try:
        return int(((data or {}).get("data") or {}).get("tc") or 0)
    except Exception:  # noqa: BLE001
        return 0


def limit_stats(day: dt.date) -> dict[str, int]:
    zt = _pool(f"{C.PUSH2EX}/getTopicZTPool", day)
    dt_ = _pool(f"{C.PUSH2EX}/getTopicDTPool", day)
    return {"limit_up": zt, "limit_down": dt_}


# --------------------------------------------------------------------------------------
# 新闻
# --------------------------------------------------------------------------------------
def news(limit: int = 40) -> list[dict]:
    """7x24 快讯。多端点兜底，全部失败返回空列表。"""
    trace = str(int(time.time() * 1000))
    for url, params in (
        (f"{C.NP_LIST}/comm/web/getFastNewsList",
         # sortEnd 是 2026-11 起新增的必填参数，缺了会返回
         # "Required String parameter 'sortEnd' is not present"。首页传空串。
         {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "",
          "pageSize": limit, "req_trace": trace}),
        (f"{C.NP_LIST}/comm/web/getNewsByColumns",
         {"client": "web", "biz": "web_news_col", "column": "345", "order": 1,
          "needInteractData": 0, "page_index": 1, "page_size": limit, "req_trace": trace}),
    ):
        data = _get(url, params, timeout=15)
        d = (data or {}).get("data") or {}
        items = d.get("fastNewsList") or d.get("list") or []
        if items:
            out = []
            for it in items:
                title = (it.get("title") or it.get("summary") or "").strip()
                if not title:
                    continue
                out.append({
                    "title": title,
                    "time": it.get("showTime") or it.get("showtime") or "",
                    "url": it.get("url_unique") or it.get("url") or "",
                })
            if out:
                return out
    return []


# --------------------------------------------------------------------------------------
# ETF
# --------------------------------------------------------------------------------------
def etf_spot(workers: int = 6) -> pd.DataFrame:
    """沪深 ETF 快照。份额 ≈ 流通市值 / 价格，可用于跟踪每日申赎。"""
    base = {
        "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3", "fs": "b:MK0021",
        "fields": "f2,f3,f6,f12,f14,f20,f21,f62,f184",
    }
    first = _get_list(f"{C.PUSH2}/api/qt/clist/get", {**base, "pn": 1, "pz": 100})
    d = (first or {}).get("data") or {}
    total = int(d.get("total") or 0)
    recs: list[dict] = list(d.get("diff") or [])
    pages = min((total + 99) // 100, 20)
    if pages > 1:
        def _pg(p: int) -> list[dict]:
            r = _get_list(f"{C.PUSH2}/api/qt/clist/get", {**base, "pn": p, "pz": 100},
                          tries=2)
            return ((r or {}).get("data") or {}).get("diff") or []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for chunk in ex.map(_pg, range(2, pages + 1)):
                recs.extend(chunk)
    if not recs:
        FALLBACK_USED["etf"] = True
        return pd.DataFrame()
    df = pd.DataFrame(recs).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f6": "amount",
        "f20": "mcap", "f21": "float_mcap", "f62": "net_in", "f184": "net_ratio",
    })
    for c in df.columns:
        if c not in ("code", "name"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["price"] > 0].drop_duplicates(subset=["code"])
    df["shares"] = df["float_mcap"] / df["price"]
    return df.reset_index(drop=True)


def etf_secid(code: str) -> str:
    code = str(code)
    return f"1.{code}" if code.startswith(("5", "6")) else f"0.{code}"


# --------------------------------------------------------------------------------------
# 两融（融资余额）—— 市场级日频历史
#
# 来源：东财 datacenter `RPTA_RZRQ_LSHJ`，一条 = 一个交易日的沪深合计。
# 为什么用 datacenter 而不是 push2：两者是不同子域、限流各自独立，
# push2 被限流时 datacenter 仍可用（simonlin1212/a-stock-data 的实测结论）。
# 东财对独有数据（两融/龙虎榜/解禁）才有价值，其余仍走已有的 push2 通道。
# --------------------------------------------------------------------------------------
def margin_history(limit: int = 400) -> pd.DataFrame:
    cached = cache_read(f"margin_{limit}", 3600 * 6)
    if cached:
        return pd.DataFrame(cached)

    rows: list[dict] = []
    page = 1
    while len(rows) < limit and page <= 12:
        d = _get("https://datacenter-web.eastmoney.com/api/data/v1/get", {
            "reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL",
            "pageNumber": page, "pageSize": 100,
            "sortColumns": "DIM_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        })
        data = ((d or {}).get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        page += 1
        time.sleep(0.35)                      # 东财要求 ≥1s 会更稳，这里折中

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "DIM_DATE": "date", "RZYE": "rzye", "RZYEZB": "rzye_pct",
        "RZJME": "rzjme", "RQYE": "rqye", "LTSZ": "float_mcap"})
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for c in ("rzye", "rzye_pct", "rzjme", "rqye", "float_mcap"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in ("date", "rzye", "rzye_pct", "rzjme", "rqye") if c in df.columns]
    df = df[keep].dropna(subset=["date"]).drop_duplicates("date").sort_values("date")
    cache_write(f"margin_{limit}", df.to_dict("records"))
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------------------
# ETF 份额（季度口径）
#
# 东财 f10「规模变动」：返回的是 JS 片段（var gmbd_apidata=...），
# 不是 JSON，所以不能走 _get；直接取文本再解析表格。
# 日频份额没有公开历史接口，只有交易所官方按日文件（沪深规则不一致），
# 因此这里取季度口径，并与本地逐日累积的 etf_share 表配合使用：
# 本地有 ≥2 天就用日频，否则退回季度。
# --------------------------------------------------------------------------------------
def etf_share_hist(code: str, periods: int = 8) -> pd.DataFrame:
    code = str(code)
    cached = cache_read(f"etfshare_{code}", 3600 * 12)
    if cached:
        return pd.DataFrame(cached)
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    try:
        r = _session().get(url, params={"type": "gmbd", "code": code, "rt": "0.1"},
                           timeout=C.TIMEOUT,
                           headers={"Referer": f"https://fundf10.eastmoney.com/gmbd_{code}.html"})
        text = r.text if r.status_code == 200 else ""
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        return pd.DataFrame()

    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 4 or not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        rows.append({"date": cells[0],
                     "buy": pd.to_numeric(cells[1].replace(",", ""), errors="coerce"),
                     "sell": pd.to_numeric(cells[2].replace(",", ""), errors="coerce"),
                     "shares": pd.to_numeric(cells[3].replace(",", ""), errors="coerce")})
        if len(rows) >= periods:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["shares"]).sort_values("date").reset_index(drop=True)
    cache_write(f"etfshare_{code}", df.assign(date=df["date"].dt.strftime("%Y-%m-%d")).to_dict("records"))
    return df


# --------------------------------------------------------------------------------------
# 尾盘 30 分钟
#
# 东财 5 分钟线一次只给约 1500 根（≈32 个交易日），且不支持按 end 翻页，
# 所以拿不到长历史——只能算最近 32 日。这不足以做因子检验，
# 但可以每天累积，并且在界面上如实标注样本长度。
# --------------------------------------------------------------------------------------
def tail30_series(secid: str = "1.000001") -> pd.DataFrame:
    cached = cache_read(f"tail30_{secid}", 3600 * 2)
    if cached:
        return pd.DataFrame(cached)
    d = _get("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
        "secid": secid, "klt": 5, "fqt": 1, "lmt": 1600, "end": "20500101",
        "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56,f57",
    })
    ks = ((d or {}).get("data") or {}).get("klines") or []
    if not ks:
        return pd.DataFrame()

    recs = []
    for line in ks:
        p = line.split(",")
        if len(p) < 3:
            continue
        recs.append((pd.Timestamp(p[0]), float(p[2])))      # 时间, 收盘
    df = pd.DataFrame(recs, columns=["ts", "close"]).sort_values("ts")
    df["day"] = df["ts"].dt.normalize()
    df["hm"] = df["ts"].dt.hour * 100 + df["ts"].dt.minute

    # 必须按时间戳切 14:30，不能取"最后 6 根"：
    # 盘中只有半天的数据时，最后 6 根是 13:30~14:15，会把盘中当成尾盘。
    out = []
    for day, g in df.groupby("day"):
        g = g.sort_values("ts")
        after = g[g["hm"] >= 1430]
        before = g[g["hm"] < 1430]
        if after.empty or before.empty:
            continue                                  # 今天还没到 14:30
        base = float(before["close"].iloc[-1])
        last = float(after["close"].iloc[-1])
        if base <= 0:
            continue
        out.append({"date": day, "pct": round((last / base - 1) * 100, 3),
                    "bars": int(len(after)), "complete": int(len(g) >= 48)})
    res = pd.DataFrame(out).sort_values("date").reset_index(drop=True)
    if not res.empty:
        cache_write(f"tail30_{secid}",
                    res.assign(date=res["date"].dt.strftime("%Y-%m-%d")).to_dict("records"))
    return res


# --------------------------------------------------------------------------------------
# 磁盘缓存（用于离线回看与加速）
# --------------------------------------------------------------------------------------
def cache_write(name: str, obj: Any) -> None:
    try:
        path = C.CACHE_DIR / f"{name}.json"
        path.write_text(SJ.dumps(obj, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def cache_read(name: str, max_age_sec: float) -> Any | None:
    path = C.CACHE_DIR / f"{name}.json"
    try:
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > max_age_sec:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def health() -> dict:
    """数据源连通性自检。"""
    t0 = time.time()
    d = _get(f"{C.PUSH2}/api/qt/ulist.np/get",
             {"fltt": 2, "secids": "1.000001", "fields": "f2,f3,f12,f14"}, timeout=8)
    ok = bool((d or {}).get("data"))
    return {"ok": ok, "latency_ms": int((time.time() - t0) * 1000)}
