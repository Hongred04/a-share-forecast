"""命令行入口。

用法
    python run.py check            检查数据源连通性
    python run.py replay [--days]  重建历史回放（训练权重、产出真实胜率）
    python run.py build            生成一次完整快照到 data/state.json
    python run.py serve            启动本地服务（默认 http://127.0.0.1:8848）
    python run.py serve --lan      开放局域网（手机同网段可访问，无鉴权）
    python run.py test             静态检查 + 契约测试 + 前端渲染冒烟测试
    python run.py research deep    运行研究报告（factors / evolve / deep / rescue）
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from core import config as C
from core import engine as E
from core import safejson as SJ


def cmd_check() -> None:
    from core import datahub as D
    print("数据源连通性：", D.health())
    print("当前市场状态：", D.market_phase())
    print("下一交易日：", D.next_trading_day(time.localtime() and __import__("datetime").date.today()))


def cmd_replay(days: int) -> None:
    E.init_db()
    print("拉取数据…")
    mkt = E.load_market()
    print(f"  {len(mkt['bench_raw'])} 根指数K线，{len(mkt['sec_frames'])} 个板块，"
          f"{len(mkt['spot'])} 只个股，新闻 {len(mkt['news'])} 条")
    t0 = time.time()
    st = E.replay(mkt, days=days, reset=True)
    print(f"回放完成，用时 {time.time() - t0:.1f}s")
    print(json.dumps({k: v for k, v in st.items() if k != "series"}, ensure_ascii=False, indent=2))


def cmd_build() -> None:
    t0 = time.time()
    state = E.build_state(force_replay=False)
    (C.DATA_DIR / "state.json").write_text(
        SJ.dumps(state, default=str), encoding="utf-8")
    pl = state["prediction"]
    print(f"快照已生成（{time.time() - t0:.1f}s）→ data/state.json")
    print(f"  {pl['target']} 收红/上涨概率 {pl['prob_up']:.1%}")
    print(f"  跳空 {pl['e_gap']:+.2f}%  日内 {pl['e_intra']:+.2f}%  全天 {pl['exp_ret']:+.2f}%")
    print(f"  板块 {len(pl['sectors_up'])} 个看多 / {len(pl['stocks'])} 只个股")


def cmd_serve(host: str, port: int, lan: bool = False) -> None:
    import socket
    import uvicorn
    if lan:
        # 显式要求才绑 0.0.0.0。默认仍只听本机——这个服务没有任何鉴权，
        # 一旦绑到 0.0.0.0，同网段的任何人都能读到持仓建议与重算接口。
        host = "0.0.0.0"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:  # noqa: BLE001
            ip = "本机局域网IP"
        print(f"服务启动： http://{host}:{port}（已开放局域网）")
        print(f"  手机同网段访问： http://{ip}:{port}")
        print("  注意：本服务无鉴权，仅在可信网络下使用；用完请关掉。")
    else:
        print(f"服务启动： http://{host}:{port}")
        print("  手机访问请加 --lan（会用 0.0.0.0 监听并打印局域网地址）")
    uvicorn.run("core.server:app", host=host, port=port, log_level="info")


def cmd_research(name: str) -> None:
    """研究报告都在 research/ 包里，这里只是给一个统一入口。"""
    import runpy
    mods = {"factors": "research.factors", "evolve": "research.evolve",
            "deep": "research.deep", "rescue": "research.rescue"}
    if name not in mods:
        print("可选报告：" + " / ".join(mods))
        sys.exit(2)
    runpy.run_module(mods[name], run_name="__main__")


def cmd_test() -> None:
    """一条命令跑完所有可自动化的检查，改代码后先跑它。"""
    import importlib.util
    import shutil
    import subprocess

    def has_module(name: str) -> bool:
        return importlib.util.find_spec(name) is not None

    # 桌面版自带的是精简运行时（只装 requirements.txt），没有 pyflakes 与 node。
    # 缺工具时应该跳过而不是判失败——否则在打包环境里 `run.py test` 永远红。
    steps = []
    if has_module("pyflakes"):
        steps.append(("静态检查（pyflakes）",
                      [sys.executable, "-m", "pyflakes", "core", "run.py"]))
    else:
        print("── 静态检查 " + "─" * 36 + "\n   跳过：未安装 pyflakes")
    steps.append(("交易日历（休市日 / 到期提醒）", [sys.executable, "tests/trading_calendar.py"]))
    steps.append(("个股校准（口径一致性 / 恒等性）", [sys.executable, "tests/stock_calib.py"]))
    steps.append(("数据健壮性（NaN / 备用源 / 板块过滤）", [sys.executable, "tests/data_robust.py"]))
    steps.append(("缠论结构（几何不变量）", [sys.executable, "tests/chan_geo.py"]))
    steps.append(("契约测试（前后端字段与取值）", [sys.executable, "tests/contract.py"]))
    if shutil.which("node"):
        steps.append(("渲染冒烟（node 里跑各区块）", ["node", "tests/ui_smoke.js"]))
    else:
        print("── 渲染冒烟 " + "─" * 36 + "\n   跳过：未安装 node")

    bad = 0
    for name, cmd in steps:
        print(f"\n── {name} " + "─" * max(0, 46 - len(name)))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            print("   跳过：未找到可执行文件")
            continue
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            print("   " + (out.strip().splitlines() or ["通过"])[-1])
        else:
            bad += 1
            print(out.strip()[:1600] or f"   退出码 {r.returncode}")
    print()
    print("全部通过" if not bad else f"有 {bad} 项未通过")
    sys.exit(1 if bad else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="A股趋势预判与自进化系统")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check")
    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--days", type=int, default=C.REPLAY_DAYS)
    sub.add_parser("build")
    sub.add_parser("test")
    p_res = sub.add_parser("research")
    p_res.add_argument("name", nargs="?", default="deep")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8848)
    p_serve.add_argument("--lan", action="store_true",
                         help="开放局域网访问（手机同网段可看）。无鉴权，慎用")
    args = ap.parse_args()

    if args.cmd == "check":
        cmd_check()
    elif args.cmd == "replay":
        cmd_replay(args.days)
    elif args.cmd == "build":
        cmd_build()
    elif args.cmd == "test":
        cmd_test()
    elif args.cmd == "research":
        cmd_research(args.name)
    elif args.cmd == "serve":
        cmd_serve(args.host, args.port, lan=args.lan)
    else:
        ap.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
