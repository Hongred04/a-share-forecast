"""把一份可移植的 Python 运行时打进行包，让桌面版不依赖系统 PATH。

为什么需要它
------------
这台机器 PATH 上唯一的 `python` 是微软商店的占位程序（WindowsApps\\python.exe，
执行后返回 9009），`py` 启动器也没装。真正装了依赖的解释器是 WorkBuddy 自带的
vendor 运行时，它指到 E: 盘，cmd 看不到。于是双击 `启动.bat` 必然报
"Python packages missing"，而用户无从判断该装什么。

做法
----
复制一份解释器骨架（不含 site-packages），再用 pip 只安装本项目需要的包到
`runtime/python/Lib/site-packages`。结果是一个自包含目录：

    runtime/python/python.exe        + Lib/ + DLLs/ + site-packages/

之后 `启动.bat` 优先用它，`python.txt` / 系统 python / `py -3` 只作为后备。

用法
----
    python packaging/build_runtime.py --dest "C:/Users/macro/Desktop/A股预判"
    python packaging/build_runtime.py --dest .        # 打在本项目里

注意
----
· 只复制骨架，不复制 vendor 的 site-packages——那里有 PIL/pygments/pytest 等
  与本项目无关的包（约 60MB），而且版本组合是 WorkBuddy 的需要，不是我们的。
· 目标机若已有完整 Python，仍可用 `python.txt` 指定，脚本不强制使用私有运行时。
· 构建是**幂等覆盖**，不删除任何文件：环境的安全策略会拦截批量删除（一次超过
  50 个文件即要求确认），所以「删掉 pandas/tests 省 30MB」这类优化在这里做不了。
  代价是运行时约 200MB，换来的是构建不会中途被拦。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 解释器骨架里必须原样带走的文件与目录
ROOT_FILES = (
    "python.exe", "pythonw.exe", "python3.exe", "python3.dll",
    "python313.dll", "python312.dll", "python311.dll", "python310.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
)
ROOT_DIRS = ("DLLs", "Lib")

# stdlib 里用不到、但体积不小的目录（删掉可省 ~12MB）
SKIP_LIB = {
    "site-packages", "test", "tests", "__pycache__",
    "idlelib", "turtledemo", "lib2to3", "ensurepip",
}


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.0f}MB"


def dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def find_vendor_python() -> Path:
    """找到一份可用的 CPython 安装目录（当前解释器优先）。"""
    exe = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    if (prefix / "Lib").is_dir() and (prefix / "python.exe").is_file():
        return prefix
    raise SystemExit(f"无法从 {exe} 推断解释器目录")


def copy_skeleton(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        s = src / name
        if s.is_file():
            shutil.copy2(s, dst / name)
    for name in ROOT_DIRS:
        s = src / name
        if not s.is_dir():
            continue
        if name == "Lib":
            copy_lib(s, dst / "Lib")
        else:
            for root, _d, files in os.walk(s):
                rel = Path(root).relative_to(s)
                (dst / name / rel).mkdir(parents=True, exist_ok=True)
                for f in files:
                    shutil.copy2(Path(root) / f, dst / name / rel / f)
    (dst / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def copy_lib(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)   # 顶层文件也要有父目录，否则 copy2 报 WinError 3
    for entry in src.iterdir():
        if entry.name in SKIP_LIB:
            continue
        target = dst / entry.name
        if entry.is_dir():
            if entry.name == "__pycache__":
                continue
            # 逐层复制，跳过任何 __pycache__
            for root, dirs, files in os.walk(entry):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                rel = Path(root).relative_to(src)
                (dst / rel).mkdir(parents=True, exist_ok=True)
                for f in files:
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    shutil.copy2(Path(root) / f, dst / rel / f)
        else:
            shutil.copy2(entry, target)


def pip_install(host_py: Path, site: Path, req: Path) -> None:
    """用源解释器执行 pip，把包装进目标的 site-packages。

    不能用目标解释器跑 pip：骨架里不含 site-packages，也就没有 pip，
    而 `ensurepip` 已被裁掉（它自带一份 pip，纯属重复）。
    源与目标同为同一版本的 CPython，wheel 标签一致，因此这样做是安全的。
    """
    cmd = [
        str(host_py), "-m", "pip", "install", "--no-cache-dir",
        "--no-warn-script-location", "--disable-pip-version-check",
        "--target", str(site), "-r", str(req),
    ]
    print("    " + " ".join(cmd[:6]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print("      " + line)
    if r.returncode != 0:
        print((r.stderr or "")[-1200:])
        raise SystemExit("pip install 失败")


def verify(py: Path, app: Path) -> None:
    """用干净环境启动，确认不依赖外部 PYTHONPATH。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    checks = [
        ("依赖导入", [str(py), "-c",
                      "import fastapi,uvicorn,pandas,numpy,requests;print(pandas.__version__)"]),
        ("本项目导入", [str(py), "-c", "from core import engine;print('core ok')"]),
    ]
    for name, cmd in checks:
        r = subprocess.run(cmd, cwd=str(app), env=env, capture_output=True, text=True)
        status = "OK" if r.returncode == 0 else "FAIL"
        print(f"    {name}: {status}  {(r.stdout or r.stderr).strip().splitlines()[-1][:80]}")
        if r.returncode != 0:
            raise SystemExit(f"{name} 失败")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=".", help="把 runtime/ 放到的目录（桌面包根目录）")
    ap.add_argument("--skip-pip", action="store_true", help="只复制骨架，不装依赖")
    a = ap.parse_args()

    app = Path(a.dest).resolve()
    if not (app / "run.py").is_file():
        raise SystemExit(f"{app} 里没有 run.py，看起来不是应用目录")

    src = find_vendor_python()
    dst = app / "runtime" / "python"
    base_py = dst / "python.exe"
    site = dst / "Lib" / "site-packages"

    print(f"  源解释器 : {src}")
    print(f"  目标     : {dst}")

    if dst.exists():
        print("  复用已有目录（幂等覆盖，不做删除）")

    print("  复制解释器骨架 ...")
    copy_skeleton(src, dst)

    if not a.skip_pip:
        print("  安装依赖（只装本项目需要的） ...")
        pip_install(src / "python.exe", site, app / "requirements.txt")

    total = dir_size(dst)
    print(f"  运行时体积: {human(total)}")

    print("  验证 ...")
    verify(base_py, app)

    print("\n  完成。启动.bat 会优先使用 runtime\\python\\python.exe")


if __name__ == "__main__":
    main()
