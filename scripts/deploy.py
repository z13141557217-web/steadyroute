#!/usr/bin/env python3
"""Validated, atomic local deployment and rollback for SteadyRoute."""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import plistlib
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
PRODUCTION_TARGET = pathlib.Path(
    "/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router"
)
PRODUCTION_BACKUPS = pathlib.Path(
    "/Users/nurture/Library/Application Support/Clash-Verge-Stability-Router Backups"
)
PRODUCTION_PLIST = pathlib.Path(
    "/Users/nurture/Library/LaunchAgents/com.nurture.clash-stability-router.plist"
)
DEFAULT_LABEL = "com.nurture.clash-stability-router"
DEFAULT_STATUS_URL = "http://127.0.0.1:17654/api/status"
REQUIRED_GROUPS = ("香港家宽自动备援", "AI 台湾家宽线路")
MANAGED_FILES = {
    "src/weighted_router.py": "weighted_router.py",
    "src/dashboard.html": "dashboard.html",
    "config/groups.yaml": "config/groups.yaml",
    "VERSION": "VERSION",
    "GIT_COMMIT": "GIT_COMMIT",
    "RELEASE.json": "RELEASE.json",
    "MANIFEST.sha256": "MANIFEST.sha256",
}


class DeploymentError(RuntimeError):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command, check=True, capture=False):
    environment = None
    if pathlib.Path(str(command[0])).name == "git":
        environment = os.environ.copy()
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        environment.pop("GIT_INDEX_FILE", None)
    return subprocess.run(
        [str(item) for item in command],
        check=check,
        text=True,
        env=environment,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def ensure_clean_worktree(project_dir):
    result = run(["git", "-C", project_dir, "status", "--porcelain=v1"], capture=True)
    if result.stdout.strip():
        raise DeploymentError("工作树不干净；提交或清理变更后再部署")


def run_project_checks(project_dir):
    run([project_dir / "scripts" / "check.sh"])


def validate_groups_config(path):
    text = path.read_text(encoding="utf-8")
    if "\t" in text:
        raise DeploymentError("groups.yaml 不能包含 tab 缩进")
    if not text.lstrip().startswith("#") or "prepend:" not in text or "append:" not in text or "delete:" not in text:
        raise DeploymentError("groups.yaml 缺少增强配置顶层字段")
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            name = stripped.split(":", 1)[1].strip().strip("'\"")
            if not name or name in names:
                raise DeploymentError("groups.yaml 包含空或重复代理组")
            names.append(name)
    missing = [name for name in REQUIRED_GROUPS if name not in names]
    if missing:
        raise DeploymentError("groups.yaml 缺少必需代理组: %s" % ", ".join(missing))


def parse_manifest(path):
    entries = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        digest, separator, relative = raw_line.partition("  ")
        if not separator or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise DeploymentError("MANIFEST.sha256 格式无效")
        relative_path = pathlib.PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative in entries:
            raise DeploymentError("MANIFEST.sha256 包含不安全或重复路径")
        entries[relative] = digest
    return entries


def validate_release_tree(root):
    required = list(MANAGED_FILES) + ["deploy/com.nurture.clash-stability-router.plist"]
    for relative in required:
        if not (root / relative).is_file():
            raise DeploymentError("发布包缺少 %s" % relative)
    entries = parse_manifest(root / "MANIFEST.sha256")
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256"
    }
    if set(entries) != actual_files:
        raise DeploymentError("发布包文件集合与 MANIFEST.sha256 不一致")
    for relative, expected in entries.items():
        if sha256(root / relative) != expected:
            raise DeploymentError("发布包文件校验失败: %s" % relative)

    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    commit = (root / "GIT_COMMIT").read_text(encoding="utf-8").strip()
    metadata = json.loads((root / "RELEASE.json").read_text(encoding="utf-8"))
    if metadata.get("version") != version or metadata.get("commit") != commit:
        raise DeploymentError("版本、commit 与 RELEASE.json 不一致")
    if not version or not commit:
        raise DeploymentError("发布版本或 commit 为空")
    py_compile.compile(str(root / "src/weighted_router.py"), doraise=True)
    with (root / "deploy/com.nurture.clash-stability-router.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    if plist.get("Label") != DEFAULT_LABEL:
        raise DeploymentError("LaunchAgent Label 不正确")
    validate_groups_config(root / "config/groups.yaml")
    return metadata


def extract_and_validate(archive, checksum_file, staging_parent):
    if not archive.is_file() or not checksum_file.is_file():
        raise DeploymentError("发布包或 SHA-256 文件不存在")
    expected = checksum_file.read_text(encoding="utf-8").split()[0]
    if sha256(archive) != expected:
        raise DeploymentError("发布包 SHA-256 不匹配")
    stage = pathlib.Path(tempfile.mkdtemp(prefix="steadyroute-release-", dir=str(staging_parent)))
    with zipfile.ZipFile(str(archive)) as bundle:
        for member in bundle.infolist():
            member_path = pathlib.PurePosixPath(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise DeploymentError("发布包包含不安全路径")
        bundle.extractall(str(stage))
    roots = [item for item in stage.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise DeploymentError("发布包必须只有一个根目录")
    metadata = validate_release_tree(roots[0])
    return stage, roots[0], metadata


def tree_hashes(root):
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def backup_checksum(files):
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_backup(target, plist_path, backup_dir):
    version = (target / "VERSION").read_text(encoding="utf-8").strip() if (target / "VERSION").is_file() else "unknown"
    commit = (target / "GIT_COMMIT").read_text(encoding="utf-8").strip() if (target / "GIT_COMMIT").is_file() else "unknown"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / ("%s-%s-%s" % (stamp, version, commit[:12]))
    suffix = 1
    while destination.exists():
        destination = backup_dir / ("%s-%s-%s-%d" % (stamp, version, commit[:12], suffix))
        suffix += 1
    payload = destination / "payload"
    payload.mkdir(parents=True)
    application = payload / "application"
    if target.exists():
        shutil.copytree(str(target), str(application))
    if plist_path.exists():
        shutil.copy2(str(plist_path), str(payload / "launchagent.plist"))
    files = tree_hashes(payload)
    metadata = {
        "schema_version": 1,
        "version": version,
        "commit": commit,
        "created_at": utc_now(),
        "files": files,
        "sha256": backup_checksum(files),
    }
    atomic_write(destination / "backup.json", (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return destination, metadata


def validate_backup(backup):
    metadata_path = backup / "backup.json"
    if not metadata_path.is_file():
        raise DeploymentError("备份缺少 backup.json: %s" % backup)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    files = tree_hashes(backup / "payload")
    if files != metadata.get("files") or backup_checksum(files) != metadata.get("sha256"):
        raise DeploymentError("备份 SHA-256 校验失败: %s" % backup)
    return metadata


def prepare_target(release_root, current_target, parent):
    staged = pathlib.Path(tempfile.mkdtemp(prefix=".steadyroute-target-", dir=str(parent)))
    if current_target.exists():
        shutil.copytree(str(current_target), str(staged), dirs_exist_ok=True)
    for source_relative, target_relative in MANAGED_FILES.items():
        destination = staged / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(release_root / source_relative), str(destination))
    return staged


def replace_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.deploy-%d" % (destination.name, os.getpid()))
    shutil.copy2(str(source), str(temporary))
    os.replace(str(temporary), str(destination))


class ServiceRuntime:
    def __init__(self, launchctl, label, plist_path, status_url, max_age, timeout):
        self.launchctl = launchctl
        self.label = label
        self.plist_path = plist_path
        self.status_url = status_url
        self.max_age = max_age
        self.timeout = timeout
        self.domain = "gui/%d" % os.getuid()

    def stop(self):
        run([self.launchctl, "bootout", "%s/%s" % (self.domain, self.label)], check=False, capture=True)

    def start(self):
        run([self.launchctl, "bootstrap", self.domain, self.plist_path], capture=True)
        run([self.launchctl, "kickstart", "-k", "%s/%s" % (self.domain, self.label)], capture=True)

    def health(self):
        printed = run([self.launchctl, "print", "%s/%s" % (self.domain, self.label)], capture=True)
        if "state = running" not in printed.stdout:
            raise DeploymentError("LaunchAgent 未处于 running 状态")
        deadline = time.time() + self.timeout
        last_error = None
        while time.time() <= deadline:
            try:
                request = urllib.request.Request(self.status_url, headers={"Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=min(3, self.timeout)) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self._validate_status(payload)
                return payload
            except Exception as error:
                last_error = error
                time.sleep(0.2)
        raise DeploymentError("健康检查失败: %s" % last_error)

    def _validate_status(self, payload):
        service = payload.get("service") or {}
        if service.get("status") != "running":
            raise DeploymentError("服务状态不是 running")
        updated_at = int(service.get("updated_at") or 0)
        if updated_at <= 0 or time.time() - updated_at > self.max_age:
            raise DeploymentError("状态数据已过期")
        groups = {item.get("name"): item for item in payload.get("groups") or []}
        for name in REQUIRED_GROUPS:
            group = groups.get(name)
            if not group:
                raise DeploymentError("状态接口缺少代理组: %s" % name)
            current = group.get("current")
            if not current or current not in (group.get("candidates") or []):
                raise DeploymentError("代理组当前节点无效: %s" % name)


def is_production(target, plist_path):
    return target.resolve() == PRODUCTION_TARGET.resolve() or plist_path.resolve() == PRODUCTION_PLIST.resolve()


def confirm_first_production(target):
    marker = target / ".steadyroute-deploy.json"
    if marker.exists():
        return
    if not sys.stdin.isatty():
        raise DeploymentError("首次生产部署必须在交互终端再次确认")
    phrase = "首次部署 %s" % target
    print("首次生产写入将修改 %s" % target)
    answer = input("请输入“%s”继续: " % phrase)
    if answer != phrase:
        raise DeploymentError("首次生产部署未获确认")


def write_deploy_marker(target, release, archive_sha):
    marker = {
        "schema_version": 1,
        "version": release["version"],
        "commit": release["commit"],
        "deployed_at": utc_now(),
        "release_sha256": archive_sha,
    }
    atomic_write(target / ".steadyroute-deploy.json", (json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def restore_previous(target, plist_path, previous_slot, backup, runtime, evidence_dir, reason):
    runtime.stop()
    if target.exists():
        failed = evidence_dir / ("failed-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        os.replace(str(target), str(failed))
        atomic_write(failed / "DEPLOYMENT_FAILURE.txt", (reason + "\n").encode("utf-8"))
    if previous_slot and previous_slot.exists():
        os.replace(str(previous_slot), str(target))
    elif target.exists():
        shutil.rmtree(str(target))
    backup_plist = backup / "payload/launchagent.plist"
    if backup_plist.exists():
        replace_file(backup_plist, plist_path)
    runtime.start()
    runtime.health()


def deploy(args):
    project = pathlib.Path(args.project_dir).resolve()
    target = pathlib.Path(args.target_dir).resolve()
    backups = pathlib.Path(args.backup_dir).resolve()
    plist_path = pathlib.Path(args.launch_agent_path).resolve()
    archive = pathlib.Path(args.package).resolve()
    checksum = pathlib.Path(args.checksum).resolve()
    production = is_production(target, plist_path)
    common = pathlib.Path(os.path.commonpath([str(target), str(backups)]))
    if common == target or common == backups:
        raise DeploymentError("目标目录与备份目录不能互相包含")
    if args.skip_project_checks and production:
        raise DeploymentError("生产部署禁止跳过项目检查")
    if not args.skip_project_checks:
        ensure_clean_worktree(project)
        run_project_checks(project)
    dry_root = None
    if args.apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        work_parent = target.parent
    else:
        dry_root = pathlib.Path(tempfile.mkdtemp(prefix="steadyroute-dry-run-"))
        work_parent = dry_root
    staging, release_root, release = extract_and_validate(archive, checksum, work_parent)
    staged_target = None
    try:
        source_version = (project / "VERSION").read_text(encoding="utf-8").strip()
        source_commit = run(["git", "-C", project, "rev-parse", "HEAD"], capture=True).stdout.strip()
        if release["version"] != source_version or release["commit"] != source_commit:
            raise DeploymentError("发布包版本/commit 与当前仓库不一致")
        if production and args.apply:
            tags = run(["git", "-C", project, "tag", "--points-at", "HEAD"], capture=True).stdout.splitlines()
            if "v%s" % source_version not in tags:
                raise DeploymentError("生产部署要求当前 commit 带有 v%s 标签" % source_version)
        current_source = target if args.apply else work_parent / "empty-current"
        staged_target = prepare_target(release_root, current_source, work_parent)
        validate_groups_config(staged_target / "config/groups.yaml")
        py_compile.compile(str(staged_target / "weighted_router.py"), doraise=True)
        with (release_root / "deploy/com.nurture.clash-stability-router.plist").open("rb") as handle:
            plistlib.load(handle)
        print("DRY-RUN validated version=%s commit=%s sha256=%s" % (release["version"], release["commit"], sha256(archive)))
        print("DRY-RUN target=%s backup_dir=%s" % (target, backups))
        if not args.apply:
            return
        if production:
            confirm_first_production(target)
        runtime = ServiceRuntime(args.launchctl_bin, args.label, plist_path, args.status_url, args.max_status_age, args.health_timeout)
        backup = None
        backup_meta = None
        previous_slot = target.parent / (".%s.previous-%d" % (target.name, os.getpid()))
        if previous_slot.exists():
            raise DeploymentError("原子替换暂存路径已存在: %s" % previous_slot)
        runtime.stop()
        activated = False
        try:
            backup, backup_meta = create_backup(target, plist_path, backups)
            validate_backup(backup)
            if target.exists():
                os.replace(str(target), str(previous_slot))
            os.replace(str(staged_target), str(target))
            activated = True
            replace_file(release_root / "deploy/com.nurture.clash-stability-router.plist", plist_path)
            write_deploy_marker(target, release, sha256(archive))
            runtime.start()
            if os.environ.get("STEADYROUTE_TEST_FAIL_AFTER_ACTIVATE"):
                if production:
                    raise DeploymentError("生产环境禁止故障注入")
                raise DeploymentError("injected failure after activation")
            runtime.health()
        except Exception as error:
            if previous_slot.exists():
                restore_previous(target, plist_path, previous_slot, backup, runtime, backups / "diagnostics", str(error))
                raise DeploymentError("部署失败，已自动恢复 %s@%s: %s" % (backup_meta["version"], backup_meta["commit"], error))
            if activated:
                runtime.stop()
                failed = backups / "diagnostics" / ("failed-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
                failed.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(target), str(failed))
                atomic_write(failed / "DEPLOYMENT_FAILURE.txt", (str(error) + "\n").encode("utf-8"))
                backup_plist = backup / "payload/launchagent.plist" if backup else None
                if backup_plist and backup_plist.exists():
                    replace_file(backup_plist, plist_path)
                raise DeploymentError("首次部署失败；没有上一应用版本可恢复，失败证据已保留: %s" % error)
            runtime.start()
            runtime.health()
            raise DeploymentError("部署在原子切换前失败，原版本已重新启动: %s" % error)
        else:
            if previous_slot.exists():
                shutil.rmtree(str(previous_slot))
            print("DEPLOYED version=%s commit=%s backup=%s" % (release["version"], release["commit"], backup))
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)
        if staged_target is not None:
            shutil.rmtree(str(staged_target), ignore_errors=True)
        if dry_root is not None:
            shutil.rmtree(str(dry_root), ignore_errors=True)


def newest_backup(backup_dir):
    candidates = sorted(
        (path for path in backup_dir.iterdir() if path.is_dir() and (path / "backup.json").is_file()),
        reverse=True,
    ) if backup_dir.exists() else []
    if not candidates:
        raise DeploymentError("没有可用备份")
    return candidates[0]


def rollback(args):
    target = pathlib.Path(args.target_dir).resolve()
    backups = pathlib.Path(args.backup_dir).resolve()
    plist_path = pathlib.Path(args.launch_agent_path).resolve()
    selected = pathlib.Path(args.backup).resolve() if args.backup else newest_backup(backups)
    metadata = validate_backup(selected)
    application = selected / "payload/application"
    if not application.is_dir():
        raise DeploymentError("备份不含上一完整应用版本")
    print("DRY-RUN rollback version=%s commit=%s backup=%s" % (metadata["version"], metadata["commit"], selected))
    if not args.apply:
        return
    staged = pathlib.Path(tempfile.mkdtemp(prefix=".steadyroute-rollback-", dir=str(target.parent)))
    shutil.copytree(str(application), str(staged), dirs_exist_ok=True)
    runtime = ServiceRuntime(args.launchctl_bin, args.label, plist_path, args.status_url, args.max_status_age, args.health_timeout)
    runtime.stop()
    previous = target.parent / (".%s.before-rollback-%d" % (target.name, os.getpid()))
    try:
        if target.exists():
            os.replace(str(target), str(previous))
        os.replace(str(staged), str(target))
        backup_plist = selected / "payload/launchagent.plist"
        if backup_plist.exists():
            replace_file(backup_plist, plist_path)
        runtime.start()
        runtime.health()
    except Exception as error:
        runtime.stop()
        if target.exists():
            failed = backups / "diagnostics" / ("failed-rollback-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
            failed.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(target), str(failed))
        if previous.exists():
            os.replace(str(previous), str(target))
        runtime.start()
        runtime.health()
        raise DeploymentError("回滚健康检查失败，已恢复回滚前版本: %s" % error)
    else:
        if previous.exists():
            diagnostic = backups / "diagnostics" / ("pre-rollback-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(previous), str(diagnostic))
        print("ROLLED BACK version=%s commit=%s" % (metadata["version"], metadata["commit"]))
    finally:
        shutil.rmtree(str(staged), ignore_errors=True)


def common_arguments(parser):
    parser.add_argument("--target-dir", default=str(PRODUCTION_TARGET))
    parser.add_argument("--backup-dir", default=str(PRODUCTION_BACKUPS))
    parser.add_argument("--launch-agent-path", default=str(PRODUCTION_PLIST))
    parser.add_argument("--launchctl-bin", default="/bin/launchctl")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--max-status-age", type=int, default=90)
    parser.add_argument("--health-timeout", type=float, default=15)
    parser.add_argument("--apply", action="store_true", help="执行真实写入；省略时仅 dry-run")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy_parser = subparsers.add_parser("deploy", help="校验并部署发布包")
    common_arguments(deploy_parser)
    deploy_parser.add_argument("--project-dir", default=str(PROJECT_DIR))
    deploy_parser.add_argument("--package", required=True)
    deploy_parser.add_argument("--checksum", required=True)
    deploy_parser.add_argument("--skip-project-checks", action="store_true", help=argparse.SUPPRESS)
    rollback_parser = subparsers.add_parser("rollback", help="恢复最近或指定的完整备份")
    common_arguments(rollback_parser)
    rollback_parser.add_argument("--backup", help="指定备份目录；默认选择最新备份")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "deploy":
            deploy(args)
        else:
            rollback(args)
    except (DeploymentError, OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
