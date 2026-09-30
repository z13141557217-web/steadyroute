#!/usr/bin/env python3
"""Build the release package: dist/SteadyRoute-v<VERSION>.zip

The zip has the same layout as the repository (VERSION, src/steadyroute/…, config/,
scripts/installer.py, install.command, uninstall.command), so the one installer works the
same from a download and from a git clone. Only what the installer needs goes in, and the
finished tree is scanned for personal data before it is zipped.

    python3 scripts/build_package.py [--out DIR]
"""

import argparse
import json
import pathlib
import re
import shutil
import stat
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import installer  # noqa: E402

TOP_FILES = ("install.command", "uninstall.command")

# Anything here means the package would carry someone's machine, subscription or nodes.
PERSONAL = [
    (re.compile(r"/Users/(?!Shared\b)[A-Za-z0-9._-]+"), "本机用户路径"),
    (re.compile(r"nurture", re.I), "个人用户名"),
    (re.compile(r"hinet家宽🇨🇳|家宽🇨🇳hy2|苏菲家宽|Verve", re.I), "个人节点或分组名"),
    (re.compile(r"https?://[^\s\"']*(?:subscribe|sub\?|/api/v1/client|token[=])", re.I), "订阅链接"),
    (re.compile(r"(?:token|password|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}", re.I), "密钥"),
    (re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|qq|163|outlook|icloud|hotmail)\.com", re.I), "邮箱"),
]
FORBIDDEN_NAMES = {"state.json", "groups.yaml", "route-policies.json", "clash-applied.json",
                   "ai-rules.json", "router.lock"}

README = """稳航 SteadyRoute v{version}

它做什么
  照看 Clash Verge 里正在使用的线路。每个分组按当前节点的国家锁定，之后只在该国家的
  家宽节点之间自动切换：节点断开几秒内换走，出现明显更快更稳的家宽时，在不打断连接的
  时机换过去。永远不会换到其他国家的节点。

  可选：AI 家宽专线。在设置页选一个国家，稳航会在 Clash Verge 里建立一条只含该国家
  家宽节点的专线，并把 ChatGPT、Claude 等 AI 服务的规则放在最前面（规则以
  ip.net.coffee 为第一优先级，每周自动同步）。写入前会展示改动，一键即可完全撤销。

安装（macOS，需要 Clash Verge Rev）
  1. 解压后，右键点 install.command，选“打开”（第一次需要这样，之后双击即可）。
  2. 如提示安装“命令行工具”，点安装，完成后再打开一次 install.command。
  3. 装好后会自动打开看板：http://127.0.0.1:{port}/
     开机后自动运行，无需再操作。
  4. 刚装好的约 1 分钟内，稳航在给家宽节点测速；有了备用节点后，断线自动切换开始生效。

换国家：在 Clash Verge 里把分组切到该国家的节点，稳航会改锁到新国家。
设置：看板右上角“设置”，可开关 AI 家宽专线、添加域名、排除分组、做 AI 分流体检。
升级 / 换电脑：用新版再运行一次 install.command，状态和设置都会保留。
卸载：运行 uninstall.command，稳航写进 Clash 的专线和规则会一并去掉。

所有数据只在这台 Mac 上，看板只有本机能打开。
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


def copy(source, target, executable=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))
    if executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build(out_dir, root=ROOT):
    root = pathlib.Path(root)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    name = "SteadyRoute-v%s" % version
    out_dir = pathlib.Path(out_dir)
    tree = out_dir / name
    shutil.rmtree(str(tree), ignore_errors=True)
    tree.mkdir(parents=True)
    for file_name in installer.APP_FILES:
        copy(root / "src" / "steadyroute" / file_name, tree / "src" / "steadyroute" / file_name)
    config = json.loads((root / installer.DEFAULT_CONFIG).read_text(encoding="utf-8"))
    if not installer.is_current_config(config) or config.get("policies"):
        raise SystemExit("default config must be the auto_lock profile with no hand-written policies")
    copy(root / installer.DEFAULT_CONFIG, tree / installer.DEFAULT_CONFIG)
    copy(root / "scripts" / "installer.py", tree / "scripts" / "installer.py", executable=True)
    for file_name in TOP_FILES:
        copy(root / file_name, tree / file_name, executable=True)
    (tree / "VERSION").write_text(version + "\n", encoding="utf-8")
    (tree / "使用说明.txt").write_text(README.format(version=version, port=installer.PORT), encoding="utf-8")

    findings = check_personal_data(tree)
    if findings:
        for relative, reason in findings:
            print("personal data: %s: %s" % (relative, reason), file=sys.stderr)
        raise SystemExit("package not built: personal data found")

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
    _tree, archive = build(args.out)
    print("package: %s" % archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
