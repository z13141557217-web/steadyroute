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

TOP_FILES = ("install.command", "uninstall.command", "rollback.command", "启动稳航.command", "停止稳航.command")

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
                   "subscription-authorization.json",
                   "ai-rules.json", "router.lock", "control-state.json", "control.lock", "service-instance.json"}

README = """稳航 SteadyRoute v{version}

它做什么
  照看 Clash Verge 里正在使用的线路。允许自动切换的通用分组按当前节点的国家锁定，
  在同国家的家宽节点之间检测和切换。AI 专线另有明确的出口策略，不能把所有线路都当成
  自动换节点；固定节点策略不会因性能、故障或预热完成而自动更换目标。

AI 家宽专线与出口策略
  本候选默认零探测；设置页明确选择后，所有稳航检测入口才请求选中节点。取消选择会
  停止后续及排队探测，历史结果保留原时间；Clash 自身健康检查另由 Clash 管理。
  net.coffee 检测规则只可导入本机精确授权的订阅，已有 Claude 规则复用；只探测明确
  绑定的节点。非授权订阅跳过所有写入、探测、维护、导入和旧记录重放，不注入 AI/HK
  分组或 REJECT。同名重新导入的新 UID 不继承授权；授权身份只保存在本机文件中。
  通用包不携带真实身份，缺少本机授权时保持不接管；安装升级保留已有授权文件。
  普通国内应用继续按订阅分流。三项公网 IP 未实测时显示未核实，不以规则成功代表一致。
  授权订阅的持久规则可在稳航停止后保留；切换窗口和已有会话不保证始终固定出口。
  在设置页选地区并明确保存以下三种互斥策略，受管 AI 规则排在原有规则之前。
  规则以 ip.net.coffee 为第一优先级，每周同步；写入前展示改动，经过内核校验、备份、
  重载核对和失败恢复。关闭专线会撤销稳航写入的分组与规则。

  固定节点 locked（推荐）：只允许已明确锁定的那个叶子节点。变慢、探测异常、故障、
  改名、移除或空集均不授权自动换节点；受管新连接只能继续尝试目标或拒绝，不能回退到
  备用、DIRECT 或其他国家。更换目标必须在设置中明确更新锁定，外部选择不会自动认领。
  仅故障备援 failover_only：只在确认故障或目标移除后允许同国家家宽备援，恢复后不
  自动返回原节点；不会只因性能更好而换节点。
  自动择优 adaptive：除故障备援外，还允许按性能与稳定性规则更换同国家家宽节点。
  ⚠ 仅故障备援和自动择优可能改变出口IP，存在账号风控风险。
  固定的是节点身份与受管路由边界，不等于供应商公网 IP 永久固定，也不保证公网地区、
  NAT 或不同协议出口永远相同。

规则检查与实际连接证据
  “34/34”是按当前规则、分组和节点选择推演的通过项数；国家来自节点名称，不能当作
  34 次真实请求、所有会话已固定或公网 IP 地区实测。
  设置页和看板共用核验结论，将已识别 AI 连接分为目标一致、其他出口、路径未知。
  同国家的其他节点及 DIRECT 也算其他出口；缺链或不能确认叶子属于未知。任一其他出口
  或未知存在时不显示绿色一致。没有活动连接时，只说明规则已核实、暂无可识别会话证据。
  规则与连接分别带检查时间，证据有效期 50 秒；快检不续期旧连接证据。离线、抓取失败、
  过期或页面读取失败时显示未核实。识别覆盖有限，无法识别归属的普通连接不判 AI 违规。
  旧会话可能保留规则重载前的出口，默认等待自然结束，不为状态变绿自动清理连接或 Cookie。
  需要确认实际公网地址时，查看外部检测的新结果并核对对应新连接链，不能只看节点名称。

安装（macOS，需要 Clash Verge Rev）
  1. 解压后，右键点 install.command，选“打开”（第一次需要这样，之后双击即可）。
  2. 如提示安装“命令行工具”，点安装，完成后再打开一次 install.command。
  3. 装好后会自动打开看板：http://127.0.0.1:{port}/
     新安装先保持工作进程暂停；在总开关预览并确认启动。登录启动单独设置。
  4. 刚装好不检测任何节点；先在设置中明确保存探测选择。只有已允许自动切换的线路才会按策略使用备用；固定节点
     策略在此期间也不会自动换目标。

通用分组换国家：在 Clash Verge 里选择其他国家节点后，按通用接管规则重新识别国家。
AI 固定目标变更：在设置中核对并明确更新锁定，不通过外部改选静默变更国家或目标。
总开关与独立入口
  设置顶部及看板显示已保存意愿与实际工作进程。暂停等待在途事务结束后停止全部检测、
  自动选路、规则同步与修复；保留控制页面、原目标、历史和已写入规则。暂停不是撤销固定。
  “停用并交还 Clash”先预览当前订阅的分组与规则，经明确确认后撤销自己的内容；
  固定保护会解除，原有订阅规则恢复，其他订阅的恢复资料保留。失败显示未完成。
  不清连接/Cookie，不选择其他国家或 DIRECT。重启前核对当前订阅与原锁定目标，
  目标缺席、外部选择漂移、控制器离线或预览变化时保持停用，不偷偷重新认领目标。
  轻量控制页面仍在运行；要退出它，请双击“停止稳航.command”。用“启动稳航.command”
  重新打开独立控制页面，再预览确认启动。安装目录也提供这两个入口。
  登录时启动工作进程独立保存；已经暂停时，登录、重启或 KeepAlive 都不会重新接管。
  工作进程崩溃不会自动重启；控制页面意外崩溃时子进程停止接单并退出，需重新核对。
  键盘打开预览后焦点进入预览区域，确认错误定位确认框，取消返回发起按钮。
  页面离线时停止入口也核对并卸载自身KeepAlive启动项，不以当前无进程代替完整退出。
  固定AI组显示固定策略；独立入口读取本安装目录登记的实例，确认错误不会被轮询清掉。
  控制页面不等待本机反向DNS；Clash离线也可打开控制页，启动工作仍需核对核心和目标。
  旧版本污染的撤销记录仍需依据备份校正，不能用总开关猜测原始归属。

设置：看板右上角“设置”，可开关 AI 家宽专线、添加域名、排除分组、做 AI 分流体检。
升级 / 换电脑：用新版再运行一次 install.command，状态和设置都会保留。
卸载：运行 uninstall.command，稳航写进 Clash 的专线和规则会一并去掉。
回退说明
  rollback.command 仅用于恢复从旧程序迁移时存档的旧 LaunchAgent（启动项），没有这类
  迁移存档会拒绝；它不是从同一 SteadyRoute 安装目录的版本备份任意回退到 rc.1 的命令。
  执行时会先停止当前服务并撤销稳航写入 Clash 的分组和规则，再恢复旧启动项，因此可能
  失去当前固定边界。固定策略下的版本回退由发布管理按完整备份执行：保持服务停止，成套
  核对应用、配置、状态与 Clash 分流边界，恢复并验证后再启动，不能用此命令代替。
  安装失败时安装器执行的自动恢复与 rollback.command 不同，以安装器输出及备份为准。

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
