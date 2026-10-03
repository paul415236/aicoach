#!/usr/bin/env python3
"""更新專案版號（語意化版本 MAJOR.MINOR.PATCH）。

用法：
    python bump_version.py patch        # 1.0.0 -> 1.0.1
    python bump_version.py minor        # 1.0.1 -> 1.1.0
    python bump_version.py major        # 1.1.0 -> 2.0.0
    python bump_version.py 1.2.3        # 直接指定版號
    python bump_version.py patch --tag  # 更新後同時建立 git tag v<新版號>
    python bump_version.py --show       # 只顯示目前版號

版號唯一來源是專案根目錄的 VERSION 檔；程式（server.py）與打包（aicoach.spec）都讀它。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def write_version(v):
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(v + "\n")


def bump(current, part):
    m = _SEMVER_RE.match(current)
    if not m:
        raise ValueError(f"目前版號 '{current}' 不是 MAJOR.MINOR.PATCH 格式")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"未知的遞增類型：{part}")
    return f"{major}.{minor}.{patch}"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}

    if "--show" in flags or not args:
        print(read_version())
        return 0

    arg = args[0]
    current = read_version()
    if _SEMVER_RE.match(arg):
        new = arg                     # 直接指定完整版號
    elif arg in ("major", "minor", "patch"):
        new = bump(current, arg)
    else:
        print(f"用法錯誤：'{arg}' 不是 major/minor/patch，也不是 x.y.z 版號", file=sys.stderr)
        return 2

    write_version(new)
    print(f"版號 {current} -> {new}")

    if "--tag" in flags:
        tag = f"v{new}"
        try:
            subprocess.run(["git", "tag", tag], cwd=HERE, check=True)
            print(f"已建立 git tag：{tag}（記得 git push origin {tag} 才會上傳）")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[警告] 建立 git tag 失敗：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
