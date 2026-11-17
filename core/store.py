"""存储层：SQLite 建表与迁移、元数据、学习器与预判结果的读写。

只负责"存什么、怎么存"，不含任何模型逻辑。所有其它模块通过这里落库。

"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from . import analytics as A
from . import config as C
from . import safejson as SJ

SCHEMA = """
CREATE TABLE IF NOT EXISTS pred (
    target TEXT PRIMARY KEY, asof TEXT, made_at TEXT, source TEXT,
    direction TEXT, prob_up REAL, z REAL, exp_ret REAL, exp_lo REAL, exp_hi REAL,
    vol REAL, payload TEXT,
    actual_pct REAL, actual_dir TEXT, hit INTEGER, brier REAL, evaluated INTEGER DEFAULT 0,
    e_gap REAL, e_intra REAL, p_gap_up REAL, actual_gap REAL, actual_intra REAL, gap_hit INTEGER,
    prob_raw REAL, p_intra REAL
);
CREATE TABLE IF NOT EXISTS sector_pred (
    target TEXT, asof TEXT, code TEXT, name TEXT, z REAL, prob REAL, rk INTEGER,
    actual_pct REAL, PRIMARY KEY (target, code)
);
CREATE TABLE IF NOT EXISTS stock_pred (
    target TEXT, code TEXT, name TEXT, sector TEXT, prob REAL, ref_price REAL,
    actual_pct REAL, prob_raw REAL, calibrated INTEGER DEFAULT 0,
    PRIMARY KEY (target, code)
);
CREATE TABLE IF NOT EXISTS weights (
    name TEXT PRIMARY KEY, weight REAL, hit_rate REAL, n INTEGER, updated TEXT
);
CREATE TABLE IF NOT EXISTS weight_hist (
    day TEXT, name TEXT, weight REAL, hit_rate REAL, PRIMARY KEY (day, name)
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, target TEXT, kind TEXT,
    detail TEXT, diagnosis TEXT, action TEXT
);
CREATE TABLE IF NOT EXISTS sig_log (
    asof TEXT, key TEXT, score REAL, y INTEGER, yi INTEGER, live INTEGER DEFAULT 0,
    PRIMARY KEY (asof, key)
);
CREATE TABLE IF NOT EXISTS day_log (
    asof TEXT PRIMARY KEY, ov REAL, gap REAL, intra REAL, c2c REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS etf_share (
    day TEXT, code TEXT, shares REAL, PRIMARY KEY (day, code)
);
"""

MIGRATIONS = {
    "pred": [("e_gap", "REAL"), ("e_intra", "REAL"), ("p_gap_up", "REAL"),
             ("actual_gap", "REAL"), ("actual_intra", "REAL"), ("gap_hit", "INTEGER"),
             ("prob_raw", "REAL"), ("p_intra", "REAL")],
    "sig_log": [("yi", "INTEGER")],
    "stock_pred": [("prob_raw", "REAL"), ("calibrated", "INTEGER DEFAULT 0")],
}

DEFAULT_GAP = {"a": 0.0, "b": 0.22, "s": 0.55, "n": 0}

DEFAULT_INTRA = {"mu": 0.03, "k": 0.10, "s": 1.00, "n": 0}

@contextmanager
def db():
    conn = sqlite3.connect(C.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

def meta_get(key: str, default: Any = None) -> Any:
    with db() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except Exception:  # noqa: BLE001
        return r["value"]

def meta_set(key: str, value: Any) -> None:
    with db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value, ensure_ascii=False)))

def load_learner() -> A.Learner:
    d = meta_get("learner")
    if d:
        return A.Learner.from_dict(d)
    with db() as c:
        rows = c.execute("SELECT name,weight FROM weights").fetchall()
    w = {r["name"]: float(r["weight"]) for r in rows} if rows else None
    return A.Learner(w=w)

def save_learner(lr: A.Learner, day: str = "") -> None:
    meta_set("learner", lr.to_dict())
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with db() as c:
        for k, v in lr.w.items():
            c.execute("INSERT INTO weights(name,weight,hit_rate,n,updated) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(name) DO UPDATE SET weight=excluded.weight, "
                      "n=excluded.n, updated=excluded.updated", (k, float(v), None, lr.n, now))
            if day:
                c.execute("INSERT INTO weight_hist(day,name,weight,hit_rate) VALUES(?,?,?,?) "
                          "ON CONFLICT(day,name) DO UPDATE SET weight=excluded.weight",
                          (day, k, float(v), None))

def load_weights() -> dict[str, float]:
    return load_learner().w

def gap_model() -> dict:
    return {**DEFAULT_GAP, **(meta_get("gap_model", {}) or {})}

def intra_model() -> dict:
    return {**DEFAULT_INTRA, **(meta_get("intra_model", {}) or {})}

def stock_calib() -> dict:
    """个股概率校准参数。与大盘 prob_calib 分开存，因为两者样本完全不同：

    大盘每天一条，两年就有 ~500 条，可以一次性回放拟合；
    个股只在实盘选出来后才有样本，且每天最多 9 条，只能前向累积。
    混在一起存会让"大盘样本 500 条"把"个股样本 3 条"的不足状态掩盖掉。
    """
    return {**A.DEFAULT_STOCK_CALIB, **(meta_get("stock_calib", {}) or {})}

def save_stock_calib(c: dict) -> None:
    meta_set("stock_calib", {**c, "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})

def stock_sample_count() -> int:
    """已被真实涨跌评价、且带 prob_raw 的个股行数——校准真正能用上的样本量。

    必须和 fit_stock_calib 的取数口径完全一致（两个条件缺一不可），
    否则界面会显示"43/40 已达标"而实际拟合用的是 0 条：
    条件宽了会虚报进度，窄了会漏算，两种都让那个 n/40 失去意义。

    这个值不存 meta，每次现算。理由是它属于"数据库里现在有多少可用样本"，
    是查询结果而不是状态：存起来一旦某天 review 没跑（或提前退出），
    它就会永远停在旧值上，而界面上那个 n/40 看起来很具体，
    没人会怀疑它其实早就不更新了。
    """
    with db() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM stock_pred "
            "WHERE actual_pct IS NOT NULL AND prob_raw IS NOT NULL"
        ).fetchone()[0])

def save_prediction(pl: dict) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO pred(target,asof,made_at,source,direction,prob_up,z,exp_ret,exp_lo,"
            "exp_hi,vol,payload,evaluated,e_gap,e_intra,p_gap_up,prob_raw,p_intra) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?) "
            "ON CONFLICT(target) DO UPDATE SET asof=excluded.asof,made_at=excluded.made_at,"
            "source=excluded.source,direction=excluded.direction,prob_up=excluded.prob_up,"
            "z=excluded.z,exp_ret=excluded.exp_ret,exp_lo=excluded.exp_lo,"
            "exp_hi=excluded.exp_hi,vol=excluded.vol,payload=excluded.payload,"
            "e_gap=excluded.e_gap,e_intra=excluded.e_intra,p_gap_up=excluded.p_gap_up,"
            "prob_raw=excluded.prob_raw,p_intra=excluded.p_intra",
            (pl["target"], pl["asof"], pl["made_at"], pl["source"], pl["direction"],
             pl["prob_up"], pl["z"], pl["exp_ret"], pl["exp_lo"], pl["exp_hi"], pl["vol"],
             SJ.dumps(pl), pl["e_gap"], pl["e_intra"], pl["p_gap_up"],
             pl.get("prob_up_raw", pl["prob_up"]), pl.get("p_intra_up")))
        c.executemany(
            "INSERT INTO sector_pred(target,asof,code,name,z,prob,rk) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(target,code) DO UPDATE SET z=excluded.z,prob=excluded.prob",
            [(pl["target"], pl["asof"], r["code"], r["name"], r["z"], r["prob"], 0)
             for r in pl.get("sector_ic_frame", [])])
        c.executemany(
            "INSERT INTO stock_pred(target,code,name,sector,prob,ref_price,prob_raw,calibrated) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(target,code) DO UPDATE SET prob=excluded.prob,"
            "ref_price=excluded.ref_price,prob_raw=excluded.prob_raw,"
            "calibrated=excluded.calibrated",
            [(pl["target"], s["code"], s["name"], s["sector"], s["prob"], s["price"],
              s.get("prob_raw", s["prob"]), int(bool(s.get("calibrated"))))
             for s in pl.get("stocks", [])])

