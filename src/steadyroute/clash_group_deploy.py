"""Safely deploy SteadyRoute's generated Clash Verge group enhancement."""

import hashlib
import datetime
import json
import os
import pathlib
import re
import tempfile
import subprocess
import socket
import stat
from urllib.parse import quote

import route_policy


class GroupEnhancementError(RuntimeError):
    """The enhancement target or transaction is unsafe."""


MAX_CONTROLLER_RESPONSE_BYTES = 32 * 1024 * 1024


def _decode_chunked_body(body, maximum):
    output = bytearray()
    cursor = 0
    while True:
        line_end = body.find(b"\r\n", cursor)
        if line_end < 0:
            raise GroupEnhancementError("controller response has malformed chunk framing")
        size_text = body[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise GroupEnhancementError("controller response has invalid chunk size")
        cursor = line_end + 2
        if size == 0:
            if body[cursor:cursor + 2] == b"\r\n":
                cursor += 2
            elif b"\r\n\r\n" in body[cursor:]:
                cursor = body.index(b"\r\n\r\n", cursor) + 4
            else:
                raise GroupEnhancementError("controller response has malformed chunk trailer")
            if body[cursor:]:
                raise GroupEnhancementError("controller response has bytes after chunked body")
            return bytes(output)
        if size < 0 or cursor + size + 2 > len(body):
            raise GroupEnhancementError("controller response has truncated chunk")
        output.extend(body[cursor:cursor + size])
        if len(output) > maximum:
            raise GroupEnhancementError("controller response exceeds size limit")
        cursor += size
        if body[cursor:cursor + 2] != b"\r\n":
            raise GroupEnhancementError("controller response has malformed chunk terminator")
        cursor += 2


def parse_http_response(raw, maximum=MAX_CONTROLLER_RESPONSE_BYTES):
    header, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise GroupEnhancementError("controller response has no HTTP header terminator")
    lines = header.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1."):
        raise GroupEnhancementError("controller response has invalid HTTP status line")
    try:
        status = int(parts[1])
    except ValueError:
        raise GroupEnhancementError("controller response has invalid HTTP status")
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon:
            raise GroupEnhancementError("controller response has malformed HTTP header")
        key = name.strip().lower()
        if key in headers:
            raise GroupEnhancementError("controller response has duplicate HTTP header")
        headers[key] = value.strip().lower()
    transfer = headers.get(b"transfer-encoding")
    length = headers.get(b"content-length")
    if transfer is not None and length is not None:
        raise GroupEnhancementError("controller response has conflicting body framing")
    if transfer is not None:
        if transfer != b"chunked":
            raise GroupEnhancementError("controller response uses unsupported transfer encoding")
        body = _decode_chunked_body(body, maximum)
    elif length is not None:
        try:
            expected = int(length)
        except ValueError:
            raise GroupEnhancementError("controller response has invalid content length")
        if expected < 0 or expected > maximum or len(body) != expected:
            raise GroupEnhancementError("controller response content length mismatch")
    elif len(body) > maximum:
        raise GroupEnhancementError("controller response exceeds size limit")
    return status, body


def controller_request(socket_path, method, path, payload=None):
    socket_path = pathlib.Path(socket_path)
    if socket_path.is_symlink():
        raise GroupEnhancementError("controller socket cannot be a symlink")
    try:
        metadata = socket_path.stat()
    except OSError as error:
        raise GroupEnhancementError("controller socket unavailable: %s" % error)
    if not stat.S_ISSOCK(metadata.st_mode):
        raise GroupEnhancementError("controller path is not a Unix socket")
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = [
        "%s %s HTTP/1.1" % (method, path), "Host: localhost",
        "Accept: application/json", "Connection: close",
    ]
    if body:
        request.extend(["Content-Type: application/json", "Content-Length: %d" % len(body)])
    encoded = ("\r\n".join(request) + "\r\n\r\n").encode("ascii") + body
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(socket_path))
        client.sendall(encoded)
        chunks = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONTROLLER_RESPONSE_BYTES + 65536:
                raise GroupEnhancementError("controller response exceeds size limit")
            chunks.append(chunk)
    except OSError as error:
        raise GroupEnhancementError("controller request failed: %s" % error)
    finally:
        client.close()
    return parse_http_response(b"".join(chunks))


def mihomo_staged_validator(core):
    core = pathlib.Path(core)
    if not core.is_file() or core.is_symlink() or not os.access(core, os.X_OK):
        raise GroupEnhancementError("Mihomo core must be an executable non-symlink file")

    def validate(rendered):
        with tempfile.TemporaryDirectory(prefix="steadyroute-groups-mihomo-") as directory:
            staged = pathlib.Path(directory) / "staged.yaml"
            staged.write_text(rendered, encoding="utf-8")
            result = subprocess.run(
                [str(core), "-t", "-f", str(staged)],
                cwd=directory,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=30,
            )
        if result.returncode != 0:
            raise GroupEnhancementError(
                "Mihomo staged validation failed: %s" % (result.stdout.strip() or result.returncode)
            )
    return validate


def verify_discovery_payload(config, payload):
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        raise GroupEnhancementError("/proxies payload has no proxies object")
    groups = []
    missing = []
    for policy in config["policies"]:
        name = policy["discovery_group_name"]
        group = proxies.get(name)
        if not isinstance(group, dict) or not isinstance(group.get("all"), list):
            missing.append(name)
            continue
        if group.get("hidden") is not True:
            raise GroupEnhancementError("discovery group is visible in dashboard: %s" % name)
        groups.append({
            "policy_id": policy["id"],
            "discovery_group_name": name,
            "candidate_count": len(group["all"]),
            "current": group.get("now"),
            "hidden": True,
        })
    if missing:
        raise GroupEnhancementError("discovery groups missing from /proxies: %s" % ", ".join(missing))
    return {"ready": True, "mode": config["mode"], "groups": groups}


def read_controller_proxies(socket_path):
    status, body = controller_request(socket_path, "GET", "/proxies")
    if status != 200:
        raise GroupEnhancementError("controller /proxies response is not HTTP 200")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise GroupEnhancementError("controller /proxies response is invalid JSON: %s" % error)


def capture_group_selections(config, payload):
    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        raise GroupEnhancementError("/proxies payload has no proxies object")
    selections = {}
    for policy in config["policies"]:
        group_name = policy["group_name"]
        group = proxies.get(group_name)
        if not isinstance(group, dict) or not isinstance(group.get("all"), list):
            raise GroupEnhancementError("active group missing from /proxies: %s" % group_name)
        current = group.get("now")
        if not isinstance(current, str) or current not in group["all"]:
            raise GroupEnhancementError("active group has unsafe current selection: %s" % group_name)
        selections[group_name] = current
    return selections


def put_controller_selection(socket_path, group_name, node_name):
    status, _body = controller_request(
        socket_path, "PUT", "/proxies/%s" % quote(group_name, safe=""), {"name": node_name}
    )
    if status not in {200, 204}:
        raise GroupEnhancementError("controller selection response is not HTTP 200/204")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path, data, mode=None):
    path = pathlib.Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _scalar(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _profile_bindings(text):
    if "\t" in text:
        raise GroupEnhancementError("profiles.yaml cannot contain tabs")
    current_values = []
    items = []
    item = None
    in_items = False
    items_seen = False
    item_indent = None
    direct_section = None
    option_seen = False

    def finish_item():
        nonlocal item
        if item is not None:
            items.append(item)
            item = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent % 2:
            raise GroupEnhancementError("profiles.yaml uses unsupported indentation")
        if indent == 0 and stripped.startswith("current:"):
            current_values.append(_scalar(stripped.partition(":")[2]))
            continue
        if indent == 0 and stripped.startswith("items:"):
            if items_seen or in_items or stripped.partition(":")[2].strip():
                raise GroupEnhancementError("profiles.yaml items must be one block sequence")
            in_items = True
            items_seen = True
            continue
        if not in_items:
            continue
        if stripped.startswith("- "):
            content = stripped[2:].strip()
            is_uid_entry = content.startswith("uid:")
            if item_indent is None:
                if not is_uid_entry or indent not in {0, 2}:
                    raise GroupEnhancementError("profiles.yaml items must start with a top-level - uid entry")
                item_indent = indent
            if indent == item_indent:
                if not is_uid_entry:
                    raise GroupEnhancementError("profiles.yaml top-level item must start with uid")
                finish_item()
                item = {"uid": _scalar(content.partition(":")[2])}
                direct_section = None
                option_seen = False
            # Any deeper sequence belongs to a field such as selected and is ignored.
            continue
        if item is None:
            raise GroupEnhancementError("profiles.yaml items contains content before its first uid")
        if indent <= item_indent:
            finish_item()
            in_items = False
            continue
        if ":" not in stripped:
            raise GroupEnhancementError("profiles.yaml contains unsupported complex YAML")
        key, _, raw_value = stripped.partition(":")
        key = key.strip()
        if indent == item_indent + 2:
            direct_section = key
            if key == "uid":
                raise GroupEnhancementError("profile item contains duplicate uid")
            if key == "type":
                if "type" in item:
                    raise GroupEnhancementError("profile item contains duplicate type")
                item["type"] = _scalar(raw_value)
            elif key == "option":
                if option_seen or raw_value.strip():
                    raise GroupEnhancementError("profile item option must be one plain mapping")
                option_seen = True
        elif indent == item_indent + 4 and direct_section == "option" and key == "groups":
            if "groups" in item or not raw_value.strip():
                raise GroupEnhancementError("profile item contains duplicate or empty option.groups")
            item["groups"] = _scalar(raw_value)
        # Other deeper mappings and lists are unrelated profile metadata and ignored.
    finish_item()
    if len(current_values) != 1 or not current_values[0]:
        raise GroupEnhancementError("profiles.yaml must contain exactly one current value")
    seen = set()
    for parsed in items:
        uid = parsed.get("uid")
        if not uid or uid in seen:
            raise GroupEnhancementError("profiles.yaml contains missing or duplicate item uid")
        seen.add(uid)
    return current_values[0], items


class ClashGroupEnhancementManager:
    """Resolve, validate, deploy, and roll back one current group binding."""

    def __init__(
            self, profiles_yaml, profile_dir, policy_path, generated_groups_path,
            staged_validator, post_write_validator=None, selection_reader=None,
            selection_writer=None):
        self.profiles_yaml = pathlib.Path(profiles_yaml)
        self.profile_dir = pathlib.Path(profile_dir)
        self.policy_path = pathlib.Path(policy_path)
        self.generated_groups_path = pathlib.Path(generated_groups_path)
        self.staged_validator = staged_validator
        self.post_write_validator = post_write_validator or self._verify_written_target
        self.selection_reader = selection_reader
        self.selection_writer = selection_writer

    @staticmethod
    def _verify_written_target(target, expected_sha):
        if sha256_bytes(pathlib.Path(target).read_bytes()) != expected_sha:
            raise GroupEnhancementError("written group enhancement checksum mismatch")

    def _resolve_binding(self):
        if not self.profiles_yaml.is_file() or self.profiles_yaml.is_symlink():
            raise GroupEnhancementError("profiles.yaml must be a regular non-symlink file")
        if (
            not self.profile_dir.is_dir() or self.profile_dir.is_symlink()
            or self.profile_dir.name != "profiles"
        ):
            raise GroupEnhancementError("profile-dir must be an explicit non-symlink profiles directory")
        if (
            self.profiles_yaml.name != "profiles.yaml"
            or self.profiles_yaml.parent.resolve() != self.profile_dir.parent.resolve()
        ):
            raise GroupEnhancementError("profiles.yaml must be the sibling index of profile-dir")
        current, items = _profile_bindings(self.profiles_yaml.read_text(encoding="utf-8"))
        if not current:
            raise GroupEnhancementError("profiles.yaml has no current profile")
        matches = [item for item in items if item.get("uid") == current]
        if len(matches) != 1 or matches[0].get("type") != "remote" or not matches[0].get("groups"):
            raise GroupEnhancementError("current profile has no unique option.groups binding")
        binding = matches[0]["groups"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.ya?ml)?", binding):
            raise GroupEnhancementError("option.groups binding is not a safe profile uid")
        filename = binding if binding.endswith((".yaml", ".yml")) else binding + ".yaml"
        target = self.profile_dir / filename
        if not target.is_file() or target.is_symlink():
            raise GroupEnhancementError("bound group enhancement must exist as a regular file")
        if target.parent.resolve() != self.profile_dir.resolve():
            raise GroupEnhancementError("bound group enhancement escapes profile-dir")
        return current, binding, target

    def plan(self):
        current, binding, target = self._resolve_binding()
        config = route_policy.load_policy_config(self.policy_path)
        if config["mode"] != "shadow":
            raise GroupEnhancementError("v0.4.0 group enhancement must remain shadow")
        expected = route_policy.render_enhancement_yaml(config).encode("utf-8")
        if self.generated_groups_path.is_symlink() or not self.generated_groups_path.is_file():
            raise GroupEnhancementError("generated groups source must be a regular file")
        generated = self.generated_groups_path.read_bytes()
        if generated != expected:
            raise GroupEnhancementError("generated groups source differs from route-policies.json")
        self.staged_validator(route_policy.render_staged_mihomo_config(config))
        original = target.read_bytes()
        return {
            "action": "unchanged" if original == generated else "replace",
            "mode": config["mode"],
            "current_profile_uid": current,
            "binding_uid": binding.rsplit(".", 1)[0],
            "target": str(target),
            "profile_dir": str(self.profile_dir.resolve()),
            "backup_root": str(self.profile_dir / ".steadyroute-group-backups"),
            "original_sha256": sha256_bytes(original),
            "replacement_sha256": sha256_bytes(generated),
            "source_sha256": sha256_bytes(expected),
        }

    def _prepare_backup(self, plan, original, selected_nodes):
        backup_root = pathlib.Path(plan["backup_root"])
        if backup_root.exists() and (backup_root.is_symlink() or not backup_root.is_dir()):
            raise GroupEnhancementError("backup root is not a safe directory")
        backup_root.mkdir(mode=0o700, exist_ok=True)
        if backup_root.parent.resolve() != self.profile_dir.resolve():
            raise GroupEnhancementError("backup root escapes profile-dir")
        created_at = datetime.datetime.now(datetime.timezone.utc)
        backup_id = "%s-%s" % (
            created_at.strftime("%Y%m%dT%H%M%S%fZ"), plan["original_sha256"][:12],
        )
        backup = backup_root / backup_id
        backup.mkdir(mode=0o700)
        _atomic_write(backup / "original.yaml", original, mode=0o600)
        metadata = {
            "schema_version": 1,
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "target": pathlib.Path(plan["target"]).name,
            "current_profile_uid": plan["current_profile_uid"],
            "binding_uid": plan["binding_uid"],
            "mode": plan["mode"],
            "original_sha256": plan["original_sha256"],
            "replacement_sha256": plan["replacement_sha256"],
            "source_sha256": plan["source_sha256"],
            "profiles_sha256": sha256_bytes(self.profiles_yaml.read_bytes()),
            "selected_nodes": selected_nodes,
        }
        encoded = (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(backup / "metadata.json", encoded, mode=0o600)
        return backup

    def apply(self, apply=False):
        plan = self.plan()
        if not apply:
            result = dict(plan)
            result["result"] = "dry-run"
            return result
        if plan["action"] == "unchanged":
            result = dict(plan)
            result["result"] = "unchanged"
            return result
        if self.selection_reader is None:
            raise GroupEnhancementError("apply requires a controller selection snapshot")
        config = route_policy.load_policy_config(self.policy_path)
        selected_nodes = capture_group_selections(config, self.selection_reader())
        target = pathlib.Path(plan["target"])
        original = target.read_bytes()
        replacement = self.generated_groups_path.read_bytes()
        if sha256_bytes(original) != plan["original_sha256"]:
            raise GroupEnhancementError("target changed after planning")
        if sha256_bytes(replacement) != plan["replacement_sha256"]:
            raise GroupEnhancementError("generated source changed after planning")
        backup = self._prepare_backup(plan, original, selected_nodes)
        mode = target.stat().st_mode & 0o777
        try:
            _atomic_write(target, replacement, mode=mode)
            self.post_write_validator(target, plan["replacement_sha256"])
        except Exception as error:
            try:
                _atomic_write(target, original, mode=mode)
                self._verify_written_target(target, plan["original_sha256"])
            except Exception as restore_error:
                raise GroupEnhancementError(
                    "apply failed and automatic restore failed: %s; restore: %s" % (
                        error, restore_error,
                    )
                )
            raise GroupEnhancementError("apply failed; original automatically restored: %s" % error)
        result = dict(plan)
        result.update({"result": "applied", "backup": str(backup), "selected_nodes": selected_nodes})
        return result

    def _verified_backup(self, backup, require_current_replacement=True):
        current_profile_uid, binding, target = self._resolve_binding()
        backup_root = self.profile_dir / ".steadyroute-group-backups"
        backup = pathlib.Path(backup)
        if (
            not backup.is_dir() or backup.is_symlink() or not backup_root.is_dir()
            or backup.parent.resolve() != backup_root.resolve()
        ):
            raise GroupEnhancementError("backup must be a direct non-symlink child of backup root")
        metadata_path = backup / "metadata.json"
        original_path = backup / "original.yaml"
        if (
            not metadata_path.is_file() or metadata_path.is_symlink()
            or not original_path.is_file() or original_path.is_symlink()
        ):
            raise GroupEnhancementError("backup is missing regular metadata or original file")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise GroupEnhancementError("backup metadata is invalid: %s" % error)
        original = original_path.read_bytes()
        current = target.read_bytes()
        if (
            metadata.get("schema_version") != 1 or metadata.get("target") != target.name
            or metadata.get("current_profile_uid") != current_profile_uid
            or metadata.get("binding_uid") != binding.rsplit(".", 1)[0]
        ):
            raise GroupEnhancementError("backup metadata does not match current binding")
        if sha256_bytes(original) != metadata.get("original_sha256"):
            raise GroupEnhancementError("backup original checksum mismatch")
        if require_current_replacement and sha256_bytes(current) != metadata.get("replacement_sha256"):
            raise GroupEnhancementError("current target changed since the recorded apply")
        return backup, metadata, original, current, target

    def restore_selections(self, backup, apply=False):
        backup, metadata, _original, _current, _target = self._verified_backup(backup)
        if metadata.get("mode") != "shadow":
            raise GroupEnhancementError("selection replay is only allowed for shadow deployments")
        selections = metadata.get("selected_nodes")
        if not isinstance(selections, dict) or not selections:
            raise GroupEnhancementError("backup has no controller selection snapshot")
        if self.selection_reader is None:
            raise GroupEnhancementError("selection replay requires controller access")
        config = route_policy.load_policy_config(self.policy_path)
        payload = self.selection_reader()
        current = capture_group_selections(config, payload)
        proxies = payload["proxies"]
        expected_groups = {policy["group_name"] for policy in config["policies"]}
        if set(selections) != expected_groups:
            raise GroupEnhancementError("selection snapshot does not match configured active groups")
        actions = []
        for group_name in sorted(expected_groups):
            node_name = selections[group_name]
            if not isinstance(node_name, str) or node_name not in proxies[group_name]["all"]:
                raise GroupEnhancementError("saved node is no longer in active group: %s" % group_name)
            actions.append({"group": group_name, "from": current[group_name], "to": node_name})
        result = {"result": "dry-run", "backup": str(backup), "actions": actions, "mode": "shadow"}
        if not apply:
            return result
        if self.selection_writer is None:
            raise GroupEnhancementError("selection replay apply requires a controller writer")
        changed = []
        try:
            for action in actions:
                if action["from"] == action["to"]:
                    continue
                self.selection_writer(action["group"], action["to"])
                changed.append(action)
        except Exception as error:
            recovery_errors = []
            for action in reversed(changed):
                try:
                    self.selection_writer(action["group"], action["from"])
                except Exception as recovery_error:
                    recovery_errors.append("%s: %s" % (action["group"], recovery_error))
            if recovery_errors:
                raise GroupEnhancementError(
                    "selection replay failed and recovery was partial: %s; recovery: %s" % (
                        error, "; ".join(recovery_errors),
                    )
                )
            raise GroupEnhancementError("selection replay failed; changed groups restored: %s" % error)
        result["result"] = "applied"
        return result

    def rollback(self, backup, apply=False):
        backup, metadata, original, current, target = self._verified_backup(backup)
        result = {
            "action": "restore",
            "target": str(target),
            "backup": str(backup),
            "current_sha256": metadata["replacement_sha256"],
            "restore_sha256": metadata["original_sha256"],
            "mode": metadata.get("mode"),
        }
        if not apply:
            result["result"] = "dry-run"
            return result
        mode = target.stat().st_mode & 0o777
        try:
            _atomic_write(target, original, mode=mode)
            self.post_write_validator(target, metadata["original_sha256"])
        except Exception as error:
            try:
                _atomic_write(target, current, mode=mode)
                self._verify_written_target(target, metadata["replacement_sha256"])
            except Exception as restore_error:
                raise GroupEnhancementError(
                    "rollback failed and current-file recovery failed: %s; recovery: %s" % (
                        error, restore_error,
                    )
                )
            raise GroupEnhancementError("rollback failed; current file automatically restored: %s" % error)
        result["result"] = "rolled-back"
        return result
