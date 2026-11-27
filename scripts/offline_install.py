"""从 pip 缓存离线安装包（公网索引不可用时的回退路径）。

背景：本机公网 pip 索引时通时不通（`pip install` 会长时间挂起无输出），而本机
pip 缓存里往往已存有需要的轮子——本轮 `playwright 1.60.0` / `requests 2.32.5` /
`greenlet` / `pyee` 全部命中缓存，离线装完即可跑验收（2026-11-27 实测）。

用法：
    python scripts/offline_install.py requests==2.32.5 [包名==版本 ...]

纪律：
  - 只从本机 pip 缓存（http / http-v2 body）里取轮子，不访问公网；
  - 只安装用户显式列出的包，不做传递依赖解析（--no-deps）——依赖需显式列出；
  - 安装前打印来源路径与版本，装完打印可导入验证。
"""

import glob
import os
import shutil
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
STAGE = os.path.join(ROOT, ".work", "_wheels")
os.makedirs(STAGE, exist_ok=True)

CACHE_GLOBS = [
    os.path.expandvars(r"%LOCALAPPDATA%\pip\cache\http\**\*"),
    os.path.expandvars(r"%LOCALAPPDATA%\pip\cache\http-v2\**\*.body"),
]


def cache_entries():
    seen = set()
    for pattern in CACHE_GLOBS:
        for path in glob.glob(pattern, recursive=True):
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            yield path


def wheel_meta(path):
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return None
    metas = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
    wheels = [n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")]
    if not metas or not wheels:
        return None
    name = version = ""
    for line in zf.read(metas[0]).decode("utf-8", "replace").splitlines()[:20]:
        if line.startswith("Name: ") and not name:
            name = line[6:].strip()
        elif line.startswith("Version: ") and not version:
            version = line[9:].strip()
        if name and version:
            break
    tag = ""
    for line in zf.read(wheels[0]).decode("utf-8", "replace").splitlines():
        if line.startswith("Tag: "):
            tag = line[5:].strip()
            break
    if not (name and version and tag):
        return None
    pys, abi, plat = tag.split("-")
    fname = "%s-%s-%s-%s-%s.whl" % (name.replace("-", "_"), version, pys, abi, plat)
    return name.lower().replace("-", "_"), version, fname


def find(name, version):
    for path in cache_entries():
        meta = wheel_meta(path)
        if not meta:
            continue
        pkg, ver, fname = meta
        if pkg == name.lower().replace("-", "_") and (version is None or ver == version):
            dest = os.path.join(STAGE, fname)
            if not os.path.exists(dest):
                shutil.copyfile(path, dest)
            return dest, ver
    return None, None


def main():
    targets = sys.argv[1:]
    if not targets:
        raise SystemExit("用法：python .work/offline_install.py <包名[==版本]> ...")
    failed = []
    for target in targets:
        name, _, version = target.partition("==")
        name = name.strip()
        version = version.strip() or None
        path, ver = find(name, version)
        if not path:
            print("[MISS] 缓存里没有 %s%s" % (name, ("==" + version) if version else ""))
            failed.append(target)
            continue
        proc = subprocess.run(
            [PY, "-m", "pip", "install", "--no-index", "--no-deps",
             "--disable-pip-version-check", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        ok = proc.returncode == 0
        print("[%s] %s==%s  <- %s" % ("OK" if ok else "FAIL", name, ver, os.path.basename(path)))
        if not ok:
            failed.append(target)
            print((proc.stderr or proc.stdout)[-400:])
    # 可导入验证
    for target in targets:
        name = target.split("==")[0].replace("-", "_")
        proc = subprocess.run(
            [PY, "-c", "import %s; print(getattr(%s,'__version__','?'))" % (name, name)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print("[IMPORT] %-14s %s" % (name, "OK " + proc.stdout.strip() if proc.returncode == 0 else "FAIL"))
    if failed:
        raise SystemExit("[FAIL] 未装成功：%s" % ", ".join(failed))


if __name__ == "__main__":
    main()
