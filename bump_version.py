#!/usr/bin/env python3
"""更新專案版號（語意化版本 MAJOR.MINOR.PATCH），可一鍵 commit / tag / push。

常用：
    python bump_version.py --show                 # 只顯示目前版號
    python bump_version.py patch                  # 只改 VERSION：1.0.0 -> 1.0.1
    python bump_version.py minor --release        # 改版號 + git commit + 建 tag v<版號>
    python bump_version.py minor --release --push # 再 push commit 與 tag 到遠端
    python bump_version.py 1.2.3 --release --push # 直接指定版號並發布

遞增類型：major（1.x.x->2.0.0）/ minor（x.1.x->x.2.0）/ patch（x.x.1->x.x.2），
或直接給完整的 x.y.z。

旗標：
    --release  改完版號後，自動 git add VERSION、commit、建立 tag v<版號>
               （tag 一定建在 commit 之後，指向正確的 commit）
    --push     搭配 --release 使用，把 commit 與 tag push 到 origin
    --tag      （相容舊用法）等同只建 tag，不 commit；建議改用 --release

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


def _git(*args):
    """執行 git 指令；失敗時拋出 CalledProcessError。"""
    subprocess.run(["git", *args], cwd=HERE, check=True)


def _git_ok():
    """確認目前在 git 倉庫內。"""
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=HERE,
                       check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def release(new, do_push):
    """commit VERSION、建立 tag v<new>，（可選）push。tag 建在 commit 之後。"""
    if not _git_ok():
        print("[警告] 不在 git 倉庫中，略過 commit / tag。", file=sys.stderr)
        return
    tag = f"v{new}"
    try:
        # 只提交 VERSION，避免把其他未相關的變更一起帶入
        _git("add", VERSION_FILE)
        _git("commit", "-m", f"chore: bump version to {new}")
        _git("tag", tag)              # tag 指向剛建立的 commit
        print(f"✅ 已 commit 並建立 tag {tag}")
        if do_push:
            # 取得目前分支名後 push
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=HERE, check=True, capture_output=True, text=True
            ).stdout.strip()
            _git("push", "origin", branch)
            _git("push", "origin", tag)
            print(f"🚀 已 push {branch} 與 tag {tag} 到 origin")
        else:
            print(f"（尚未 push。要上傳請加 --push，或手動：git push origin HEAD && git push origin {tag}）")
    except subprocess.CalledProcessError as e:
        print(f"[錯誤] git 操作失敗：{e}", file=sys.stderr)
        print("請檢查工作區狀態；VERSION 已更新但發布步驟未完成。", file=sys.stderr)


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

    if "--release" in flags:
        release(new, do_push="--push" in flags)
    elif "--tag" in flags:
        # 相容舊用法：只建 tag（不 commit）。注意 tag 會指向目前 HEAD。
        try:
            _git("tag", f"v{new}")
            print(f"已建立 git tag：v{new}（未 commit；建議改用 --release）")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[警告] 建立 git tag 失敗：{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
