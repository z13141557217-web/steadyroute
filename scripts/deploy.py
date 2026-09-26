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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
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
    "src/state_contract.py": "state_contract.py",
    "src/route_policy.py": "route_policy.py",
    "src/candidate_registry.py": "candidate_registry.py",
    "src/health_model.py": "health_model.py",
    "src/runtime_metrics.py": "runtime_metrics.py",
    "src/logging_setup.py": "logging_setup.py",
    "src/node_catalog.py": "node_catalog.py",
    "src/dashboard.html": "dashboard.html",
    "src/acceptance_dashboard.html": "acceptance_dashboard.html",
    "src/candidate_dashboard.html": "candidate_dashboard.html",
    "src/nodes.html": "nodes.html",
    "src/guide.html": "guide.html",
    "src/changelog.html": "changelog.html",
    "src/pages.css": "pages.css",
    "src/fixtures/status_contract_v2.json": "fixtures/status_contract_v2.json",
    "config/groups.yaml": "config/groups.yaml",
    "config/route-policies.json": "config/route-policies.json",
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
    required = list(MANAGED_FILES) + [
        "deploy/com.nurture.clash-stability-router.plist",
        "src/clash_group_deploy.py",
        "tools/manage-clash-groups.py",
        "tools/verify-clash-discovery.py",
    ]
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
    py_compile.compile(str(root / "src/state_contract.py"), doraise=True)
    py_compile.compile(str(root / "src/route_policy.py"), doraise=True)
    py_compile.compile(str(root / "src/candidate_registry.py"), doraise=True)
    py_compile.compile(str(root / "src/health_model.py"), doraise=True)
    py_compile.compile(str(root / "src/runtime_metrics.py"), doraise=True)
    py_compile.compile(str(root / "src/logging_setup.py"), doraise=True)
    py_compile.compile(str(root / "src/clash_group_deploy.py"), doraise=True)
    py_compile.compile(str(root / "tools/manage-clash-groups.py"), doraise=True)
    py_compile.compile(str(root / "tools/verify-clash-discovery.py"), doraise=True)
    json.loads((root / "src/fixtures/status_contract_v2.json").read_text(encoding="utf-8"))
    route_policies = json.loads((root / "config/route-policies.json").read_text(encoding="utf-8"))
    if route_policies.get("mode") != "shadow":
        raise DeploymentError("v0.4.0 发布包必须保持动态候选 shadow 模式")
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
        members = set()
        for member in bundle.infolist():
            member_path = pathlib.PurePosixPath(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise DeploymentError("发布包包含不安全路径")
            normalized = member_path.as_posix().rstrip("/")
            if normalized in members:
                raise DeploymentError("发布包包含重复成员: %s" % normalized)
            members.add(normalized)
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
    def __init__(self, launchctl, label, plist_path, status_url, max_age, timeout, stop_timeout):
        self.launchctl = launchctl
        self.label = label
        self.plist_path = plist_path
        self.status_url = status_url
        self.max_age = max_age
        self.timeout = timeout
        self.stop_timeout = stop_timeout
        self.domain = "gui/%d" % os.getuid()

    def _service_loaded(self):
        result = run(
            [self.launchctl, "print", "%s/%s" % (self.domain, self.label)],
            check=False,
            capture=True,
        )
        return result.returncode == 0

    def _endpoint_listening(self):
        parsed = urllib.parse.urlparse(self.status_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            connection = socket.create_connection((parsed.hostname, port), timeout=0.2)
        except OSError:
            return False
        connection.close()
        return True

    def stop(self, require_loaded=True):
        result = run(
            [self.launchctl, "bootout", "%s/%s" % (self.domain, self.label)],
            check=False,
            capture=True,
        )
        if result.returncode != 0 and require_loaded:
            raise DeploymentError("LaunchAgent bootout 失败: %s" % result.stdout.strip())
        deadline = time.time() + self.stop_timeout
        while time.time() <= deadline:
            if not self._service_loaded() and not self._endpoint_listening():
                return
            time.sleep(0.2)
        raise DeploymentError("旧服务未在 %.1f 秒内退出，拒绝继续部署" % self.stop_timeout)

    def start(self):
        run([self.launchctl, "bootstrap", self.domain, self.plist_path], capture=True)
        run([self.launchctl, "kickstart", "-k", "%s/%s" % (self.domain, self.label)], capture=True)

    def read_status(self):
        request = urllib.request.Request(self.status_url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=min(3, self.timeout)) as response:
            return json.loads(response.read().decode("utf-8"))

    def snapshot(self):
        try:
            return self.read_status()
        except Exception:
            return None

    def health(self, previous_status=None, process_started_after=None):
        printed = run([self.launchctl, "print", "%s/%s" % (self.domain, self.label)], capture=True)
        if "state = running" not in printed.stdout:
            raise DeploymentError("LaunchAgent 未处于 running 状态")
        deadline = time.time() + self.timeout
        last_error = None
        while time.time() <= deadline:
            try:
                payload = self.read_status()
                self._validate_status(payload, previous_status, process_started_after)
                return payload
            except Exception as error:
                last_error = error
                time.sleep(0.2)
        raise DeploymentError("健康检查失败: %s" % last_error)

    def _validate_status(self, payload, previous_status=None, process_started_after=None):
        service = payload.get("service") or {}
        if service.get("status") != "running":
            raise DeploymentError("服务状态不是 running")
        updated_at = int(service.get("updated_at") or 0)
        started_at = int(service.get("started_at") or 0)
        if updated_at <= 0 or time.time() - updated_at > self.max_age:
            raise DeploymentError("状态数据已过期")
        if started_at <= 0 or updated_at < started_at:
            raise DeploymentError("新进程尚未完成检测周期")
        if process_started_after is not None and started_at < int(process_started_after):
            raise DeploymentError("状态接口仍来自部署前进程")
        if previous_status:
            previous_service = previous_status.get("service") or {}
            if started_at <= int(previous_service.get("started_at") or 0):
                raise DeploymentError("服务启动时间未更新")
            if updated_at <= int(previous_service.get("updated_at") or 0):
                raise DeploymentError("部署后尚未产生新的检测结果")
        groups = {item.get("name"): item for item in payload.get("groups") or []}
        for name in REQUIRED_GROUPS:
            group = groups.get(name)
            if not group:
                raise DeploymentError("状态接口缺少代理组: %s" % name)
            current = group.get("current")
            if not current or current not in (group.get("candidates") or []):
                raise DeploymentError("代理组当前节点无效: %s" % name)


def path_contains(parent, child):
    return pathlib.Path(os.path.commonpath([str(parent), str(child)])) == parent


def paths_overlap(first, second):
    common = pathlib.Path(os.path.commonpath([str(first), str(second)]))
    return common == first or common == second


def is_production(target, backups, plist_path):
    return (
        target == PRODUCTION_TARGET.resolve()
        and backups == PRODUCTION_BACKUPS.resolve()
        and plist_path == PRODUCTION_PLIST.resolve()
    )


def validate_apply_paths(target, backups, plist_path, project, allow_non_production, non_production_root):
    production = is_production(target, backups, plist_path)
    production_members = {
        PRODUCTION_TARGET.resolve(), PRODUCTION_BACKUPS.resolve(), PRODUCTION_PLIST.resolve()
    }
    if not production and any(path in production_members for path in (target, backups, plist_path)):
        raise DeploymentError("生产 apply 必须使用全部精确默认路径")
    repositories = {project.resolve(), PROJECT_DIR.resolve()}
    forbidden = (pathlib.Path("/").resolve(), pathlib.Path.home().resolve(), *repositories)
    for label, path in (("目标", target), ("备份", backups), ("plist", plist_path)):
        if path in forbidden:
            raise DeploymentError("%s路径过于宽泛或受保护: %s" % (label, path))
    if paths_overlap(target, backups):
        raise DeploymentError("目标目录与备份目录不能互相包含")
    if any(paths_overlap(path, repository) for path in (target, backups) for repository in repositories):
        raise DeploymentError("目标或备份目录不能与源码仓库重叠")
    if paths_overlap(target, plist_path) or paths_overlap(backups, plist_path):
        raise DeploymentError("plist 不能位于目标或备份目录内")
    if (target.exists() and not target.is_dir()) or (backups.exists() and not backups.is_dir()):
        raise DeploymentError("目标和备份路径必须是目录")
    if plist_path.exists() and not plist_path.is_file():
        raise DeploymentError("plist 路径不能是目录")
    if production:
        return True
    if not allow_non_production or not non_production_root:
        raise DeploymentError("非生产 apply 必须显式提供 --allow-non-production 和 --non-production-root")
    development_root = pathlib.Path(non_production_root).resolve()
    if development_root in forbidden or any(paths_overlap(development_root, repository) for repository in repositories):
        raise DeploymentError("非生产根目录过于宽泛或与源码仓库重叠")
    for label, path in (("目标", target), ("备份", backups), ("plist", plist_path)):
        if path == development_root or not path_contains(development_root, path):
            raise DeploymentError("%s路径必须严格位于非生产根目录内" % label)
    return False


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
    runtime.stop(require_loaded=False)
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
    recovery_started_at = int(time.time())
    runtime.start()
    runtime.health(process_started_after=recovery_started_at)


def deploy(args):
    project = pathlib.Path(args.project_dir).resolve()
    target = pathlib.Path(args.target_dir).resolve()
    backups = pathlib.Path(args.backup_dir).resolve()
    plist_path = pathlib.Path(args.launch_agent_path).resolve()
    archive = pathlib.Path(args.package).resolve()
    checksum = pathlib.Path(args.checksum).resolve()
    production = is_production(target, backups, plist_path)
    if args.apply:
        production = validate_apply_paths(
            target, backups, plist_path, project,
            args.allow_non_production, args.non_production_root,
        )
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
        py_compile.compile(str(staged_target / "state_contract.py"), doraise=True)
        py_compile.compile(str(staged_target / "health_model.py"), doraise=True)
        py_compile.compile(str(staged_target / "runtime_metrics.py"), doraise=True)
        py_compile.compile(str(staged_target / "logging_setup.py"), doraise=True)
        with (release_root / "deploy/com.nurture.clash-stability-router.plist").open("rb") as handle:
            plistlib.load(handle)
        print("DRY-RUN validated version=%s commit=%s sha256=%s" % (release["version"], release["commit"], sha256(archive)))
        print("DRY-RUN target=%s backup_dir=%s" % (target, backups))
        if not args.apply:
            return
        if production:
            confirm_first_production(target)
        runtime = ServiceRuntime(
            args.launchctl_bin, args.label, plist_path, args.status_url,
            args.max_status_age, args.health_timeout, args.stop_timeout,
        )
        previous_status = runtime.snapshot()
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
            process_started_at = int(time.time())
            runtime.start()
            if os.environ.get("STEADYROUTE_TEST_FAIL_AFTER_ACTIVATE"):
                if production:
                    raise DeploymentError("生产环境禁止故障注入")
                raise DeploymentError("injected failure after activation")
            runtime.health(previous_status=previous_status, process_started_after=process_started_at)
        except Exception as error:
            if previous_slot.exists():
                restore_previous(target, plist_path, previous_slot, backup, runtime, backups / "diagnostics", str(error))
                raise DeploymentError("部署失败，已自动恢复 %s@%s: %s" % (backup_meta["version"], backup_meta["commit"], error))
            if activated:
                runtime.stop(require_loaded=False)
                failed = backups / "diagnostics" / ("failed-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
                failed.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(target), str(failed))
                atomic_write(failed / "DEPLOYMENT_FAILURE.txt", (str(error) + "\n").encode("utf-8"))
                backup_plist = backup / "payload/launchagent.plist" if backup else None
                if backup_plist and backup_plist.exists():
                    replace_file(backup_plist, plist_path)
                raise DeploymentError("首次部署失败；没有上一应用版本可恢复，失败证据已保留: %s" % error)
            recovery_started_at = int(time.time())
            runtime.start()
            runtime.health(process_started_after=recovery_started_at)
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
    project = PROJECT_DIR.resolve()
    if args.apply:
        validate_apply_paths(
            target, backups, plist_path, project,
            args.allow_non_production, args.non_production_root,
        )
    selected = pathlib.Path(args.backup).resolve() if args.backup else newest_backup(backups)
    if args.apply and (selected == backups or not path_contains(backups, selected)):
        raise DeploymentError("指定备份必须位于备份根目录内")
    metadata = validate_backup(selected)
    application = selected / "payload/application"
    if not application.is_dir():
        raise DeploymentError("备份不含上一完整应用版本")
    print("DRY-RUN rollback version=%s commit=%s backup=%s" % (metadata["version"], metadata["commit"], selected))
    if not args.apply:
        return
    staged = pathlib.Path(tempfile.mkdtemp(prefix=".steadyroute-rollback-", dir=str(target.parent)))
    shutil.copytree(str(application), str(staged), dirs_exist_ok=True)
    runtime = ServiceRuntime(
        args.launchctl_bin, args.label, plist_path, args.status_url,
        args.max_status_age, args.health_timeout, args.stop_timeout,
    )
    previous_status = runtime.snapshot()
    try:
        runtime.stop()
    except Exception:
        shutil.rmtree(str(staged), ignore_errors=True)
        raise
    previous = target.parent / (".%s.before-rollback-%d" % (target.name, os.getpid()))
    try:
        if target.exists():
            os.replace(str(target), str(previous))
        os.replace(str(staged), str(target))
        backup_plist = selected / "payload/launchagent.plist"
        if backup_plist.exists():
            replace_file(backup_plist, plist_path)
        process_started_at = int(time.time())
        runtime.start()
        runtime.health(previous_status=previous_status, process_started_after=process_started_at)
    except Exception as error:
        runtime.stop(require_loaded=False)
        if target.exists():
            failed = backups / "diagnostics" / ("failed-rollback-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
            failed.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(target), str(failed))
        if previous.exists():
            os.replace(str(previous), str(target))
        recovery_started_at = int(time.time())
        runtime.start()
        runtime.health(process_started_after=recovery_started_at)
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
    parser.add_argument("--health-timeout", type=float, default=45)
    parser.add_argument("--stop-timeout", type=float, default=5)
    parser.add_argument("--allow-non-production", action="store_true")
    parser.add_argument("--non-production-root")
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
