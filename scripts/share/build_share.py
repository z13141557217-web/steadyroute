#!/usr/bin/env python3
"""Build the share package a friend can install: dist/SteadyRoute-share-v<VERSION>.zip

Only the program, the share-edition config and the installer go in. The author's own
config (route-policies.json with his node names, groups.yaml), state, logs and test-only
pages stay out, and the finished tree is scanned for personal data before it is zipped.

    python3 scripts/share/build_share.py [--out DIR]
"""

import argparse
import json
import pathlib
import re
import shutil
import stat
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "steadyroute"

APP_FILES = (
    "weighted_router.py", "state_contract.py", "route_policy.py", "candidate_registry.py",
    "health_model.py", "runtime_metrics.py", "logging_setup.py", "node_catalog.py",
    "regions.py", "auto_lock.py",
    "dashboard.html", "nodes.html", "guide.html", "changelog.html", "pages.css",
)
SHARE_CONFIG = ROOT / "config" / "route-policies.auto-lock.json"

# Anything here means the package would carry the author's machine or subscription.
PERSONAL = [
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "本机用户路径"),
    (re.compile(r"nurture", re.I), "作者用户名"),
    (re.compile(r"hinet家宽🇨🇳|家宽🇨🇳hy2"), "作者的节点名"),
    (re.compile(r"https?://[^\s\"']*(?:subscribe|sub\?|/api/v1/client|token[=])", re.I), "订阅链接"),
    (re.compile(r"(?:token|password|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}", re.I), "密钥"),
    (re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|qq|163|outlook|icloud|hotmail)\.com", re.I), "邮箱"),
]
FORBIDDEN_NAMES = {"state.json", "groups.yaml", "route-policies.json.bak", "router.lock"}

INSTALL_COMMAND = r"""#!/bin/bash
# 双击运行：安装或升级稳航分享版。
cd "$(dirname "$0")" || exit 1
echo "稳航分享版安装"
echo
if ! xcode-select -p >/dev/null 2>&1; then
  echo "这台 Mac 还没有 Apple 命令行工具（稳航需要里面的 python3）。"
  echo "马上会弹出安装窗口，点“安装”，装好后再双击一次 install.command。"
  xcode-select --install >/dev/null 2>&1
  read -r -n 1 -p "按任意键关闭窗口"
  exit 1
fi
/usr/bin/python3 tools/installer.py install "$@"
code=$?
echo
read -r -n 1 -p "按任意键关闭窗口"
exit $code
"""

UNINSTALL_COMMAND = r"""#!/bin/bash
# 双击运行：卸载稳航分享版。Clash Verge 的设置不受影响。
cd "$(dirname "$0")" || exit 1
read -r -p "确定卸载稳航？(y/N) " answer
case "$answer" in
  y|Y) /usr/bin/python3 tools/installer.py uninstall ;;
  *) echo "已取消。" ;;
esac
echo
read -r -n 1 -p "按任意键关闭窗口"
"""

README = """稳航 SteadyRoute 分享版 v{version}

它做什么
  照看你 Clash Verge 里正在用的线路。每个分组按当前节点的国家锁定（比如美国），
  之后只在这个国家的家宽节点之间自动切换：节点断了几秒内换走，有明显更快更稳的
  家宽时不打断正在进行的对话换过去。不会换到别的国家，也不会改你的 Clash 配置。

安装（macOS，需要 Clash Verge）
  1. 解压后，右键点 install.command，选“打开”（第一次需要这样，之后双击即可）。
  2. 如果提示要装“命令行工具”，点安装，装好后再打开一次 install.command。
  3. 装好后会自动打开看板：http://127.0.0.1:{port}/
     开机后自动运行，不需要再管它。
  4. 刚装好的前 1 分钟左右，稳航在给家宽节点测速；有了备用节点后，断线自动切换才开始生效。

想换国家
  直接在 Clash Verge 里把分组切到那个国家的节点，稳航会改锁到新国家。

升级：用新版安装包再运行一次 install.command，状态和设置都会保留。
卸载：运行 uninstall.command。

所有数据只在你这台 Mac 上，看板只有本机能打开。
"""


def check_personal_data(root):
    """Return a list of (path, reason) for anything that looks personal."""
    findings = []
    for path in sorted(pathlib.Path(root).rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if path.name in FORBIDDEN_NAMES:
            findings.append((relative, "不该分发的文件"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, reason in PERSONAL:
            match = pattern.search(text)
            if match:
                findings.append((relative, "%s：%s" % (reason, match.group(0)[:60])))
    return findings


def build(out_dir):
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    name = "SteadyRoute-share-v%s" % version
    out_dir = pathlib.Path(out_dir)
    tree = out_dir / name
    shutil.rmtree(str(tree), ignore_errors=True)
    (tree / "app" / "config").mkdir(parents=True)
    (tree / "tools").mkdir()
    for file_name in APP_FILES:
        shutil.copy2(str(SRC / file_name), str(tree / "app" / file_name))
    (tree / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    config = json.loads(SHARE_CONFIG.read_text(encoding="utf-8"))
    if config.get("profile") != "auto_lock" or config.get("policies"):
        raise SystemExit("share config must be the auto_lock profile with no hand-written policies")
    (tree / "app" / "config" / "route-policies.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(str(ROOT / "scripts" / "share" / "installer.py"), str(tree / "tools" / "installer.py"))
    for file_name, content in (("install.command", INSTALL_COMMAND), ("uninstall.command", UNINSTALL_COMMAND)):
        path = tree / file_name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tree / "使用说明.txt").write_text(README.format(version=version, port=17654), encoding="utf-8")

    findings = check_personal_data(tree)
    if findings:
        for relative, reason in findings:
            print("personal data: %s: %s" % (relative, reason), file=sys.stderr)
        raise SystemExit("share package not built: personal data found")

    archive = out_dir / (name + ".zip")
    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(tree.rglob("*")):
            info = zipfile.ZipInfo.from_file(str(path), (pathlib.Path(name) / path.relative_to(tree)).as_posix())
            if path.is_dir():
                bundle.writestr(info, b"")
                continue
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, path.read_bytes())   # keeps the executable bit of the .command files
    return tree, archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "dist"))
    args = parser.parse_args(argv)
    tree, archive = build(args.out)
    print("share package: %s" % archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
