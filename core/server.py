"""FastAPI 服务：提供状态接口与单页界面。"""
from __future__ import annotations

import json
import threading
import time
import traceback

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as C
from . import engine as E
from . import safejson as SJ

app = FastAPI(title="A股趋势预判与自进化系统", docs_url=None, redoc_url=None)

_lock = threading.Lock()
_state: dict = {"data": None, "building": False, "error": None,
                "built_at": 0.0, "log": "未开始"}


def _build(force_replay: bool = False) -> None:
    with _lock:
        if _state["building"]:
            return
        _state["building"] = True
        _state["error"] = None
        _state["log"] = "拉取数据中"
    try:
        t0 = time.time()
        data = E.build_state(force_replay=force_replay)
        _state["data"] = data
        _state["built_at"] = time.time()
        dump_snapshot(data)   # 只在构建完成时落盘
        _state["log"] = f"完成，用时 {time.time() - t0:.1f}s"
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{exc}\n{traceback.format_exc()[-1500:]}"
        _state["log"] = "失败"
    finally:
        _state["building"] = False


def warm_start() -> None:
    """启动时优先读本地缓存快照，随后后台刷新。"""
    snap = C.DATA_DIR / "state.json"
    if snap.exists():
        try:
            _state["data"] = json.loads(snap.read_text(encoding="utf-8"))
            _state["built_at"] = stat_mtime(snap)
            _state["log"] = "已加载本地快照"
        except Exception:  # noqa: BLE001
            pass
    # 收盘后已经算过这一次，就不再重算——行情不会变，重启一次多等 50 秒
    # 没有任何收益，反而多打一轮接口。用户仍可手动「完整重算」。
    fresh, made = E.snapshot_fresh_after_close()
    if fresh:
        _state["log"] = f"收盘后数据已定（{made[11:16]} 算过），未重算"
        return
    threading.Thread(target=_build, daemon=True).start()


def stat_mtime(path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return 0.0


def dump_snapshot(data: dict) -> None:
    try:
        (C.DATA_DIR / "state.json").write_text(
            SJ.dumps(data, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _startup() -> None:
    warm_start()


app.mount("/static", StaticFiles(directory=str(C.WEB_DIR)), name="static")


@app.middleware("http")
async def _revalidate_assets(request, call_next):
    """让前端资源每次都回源校验（仍走 ETag，未改动时是 304）。

    不做这件事的后果很具体：改完 app.js 刷新页面，浏览器直接用缓存里的旧文件，
    看到的现象是"改了没生效"，据此去查代码会白白绕一大圈。
    默认给的是启发式缓存，没有任何版本号，所以这里显式压掉强缓存。
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(C.WEB_DIR / "index.html")


@app.get("/api/state")
def api_state():
    if _state["data"] is None:
        return JSONResponse({"ready": False, "building": _state["building"],
                             "error": _state["error"], "log": _state["log"]},
                            status_code=202)
    # 注意：不要在读取时把内存里的旧快照写回 state.json——
    # 外部重跑回放（python run.py replay）刚写好的新快照会被浏览器的轮询覆盖掉。
    return _state["data"]


@app.post("/api/refresh")
def api_refresh(full: bool = False):
    if _state["building"]:
        return {"ok": False, "msg": "正在更新中"}
    # 普通刷新：收盘后算过就不再算。要强制重算走 full=true（「完整重算」按钮）。
    if not full:
        fresh, made = E.snapshot_fresh_after_close()
        if fresh:
            return {"ok": True, "skipped": True,
                    "msg": f"收盘后数据已定（{made[11:16]} 算过），这次不重算"}
    threading.Thread(target=_build, kwargs={"force_replay": bool(full)}, daemon=True).start()
    return {"ok": True, "msg": "已开始更新" + ("（含完整回放，约 1-2 分钟）" if full else "")}


@app.get("/api/status")
def api_status():
    return {"building": _state["building"], "error": _state["error"],
            "log": _state["log"], "built_at": _state["built_at"]}


@app.get("/api/sources")
def api_sources(force: bool = False):
    """数据源健康。只返回中文名称/状态/耗时，不暴露端点。"""
    from . import sources as S
    return S.snapshot(force=force)


@app.get("/api/health")
def api_health():
    from . import datahub as D
    return D.health()
