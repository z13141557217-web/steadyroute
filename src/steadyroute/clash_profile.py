"""Write SteadyRoute's lines into Clash Verge and take them out again. Standard library only.

Where things go
  Clash Verge keeps, per subscription, an "extension" file for proxy groups and one for
  rules (profiles.yaml → option.groups / option.rules). Both are YAML with prepend / append /
  delete lists and survive subscription updates. SteadyRoute owns one marked block at the
  top of each prepend list and never touches anything outside it.

  Clash Verge only merges those files into its runtime config (clash-verge.yaml) when it
  rebuilds the profile, so the same items are also placed at the top of the runtime file,
  which is validated with Clash Verge's own core (`verge-mihomo -t`) and then loaded through
  the controller. When Clash Verge later rebuilds, the extension files produce the same result.

Every change: back up → edit → validate with the core → write → reload → verify. Any failure
restores all three files and reloads the original config.
"""

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

MARK_BEGIN = "# >>> SteadyRoute 稳航管理（请勿手动修改）"
MARK_END = "# <<< SteadyRoute 稳航管理"
APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
CORE_CANDIDATES = (
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo-alpha",
)


class ProfileError(RuntimeError):
    """Clash Verge's files are not in a state we can safely edit; nothing was changed."""


def default_home():
    return pathlib.Path(os.path.expanduser("~/Library/Application Support")) / APP_ID


def find_core():
    for path in CORE_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    return None


# ---------------------------------------------------------------- profiles.yaml
def _scalar(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def profile_bindings(text):
    """(current uid, {uid: {"type", "option": {...}}}) from Clash Verge's profiles.yaml.

    Only the parts we need are read: top-level `current`, and for each item its uid, type and
    the scalar keys under `option`. Anything else is ignored.
    """
    current = None
    items = {}
    item = None
    in_items = False
    item_indent = None
    section = None
    option_indent = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0 and not (in_items and stripped.startswith("- ")):
            in_items = stripped.startswith("items:")
            if stripped.startswith("current:"):
                current = _scalar(stripped.partition(":")[2]) or None
            item = None
            continue
        if not in_items:
            continue
        if stripped.startswith("- "):
            if item_indent is None:
                item_indent = indent
            if indent == item_indent:
                content = stripped[2:]
                item = None
                section = None
                if content.startswith("uid:"):
                    uid = _scalar(content.partition(":")[2])
                    item = items.setdefault(uid, {"type": None, "option": {}})
                continue
        if item is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        if indent == item_indent + 2 and not stripped.startswith("- "):
            section = key
            option_indent = None
            if key == "type":
                item["type"] = _scalar(value)
        elif section == "option" and indent > item_indent + 2:
            if option_indent is None:
                option_indent = indent
            if indent == option_indent and value.strip():
                item["option"][key] = _scalar(value)
    return current, items


class Target(object):
    """The extension files of the subscription Clash Verge is using right now."""

    def __init__(self, home=None):
        self.home = pathlib.Path(home or default_home())
        self.profiles_yaml = self.home / "profiles.yaml"
        self.profile_dir = self.home / "profiles"
        self.runtime = self.home / "clash-verge.yaml"

    def resolve(self):
        if not self.profiles_yaml.is_file():
            raise ProfileError("没找到 Clash Verge 的配置（%s）" % self.profiles_yaml)
        if not self.runtime.is_file():
            raise ProfileError("没找到 Clash Verge 的运行配置 clash-verge.yaml，请先打开 Clash Verge")
        current, items = profile_bindings(self.profiles_yaml.read_text(encoding="utf-8"))
        if not current or current not in items:
            raise ProfileError("Clash Verge 里没有正在使用的订阅")
        option = items[current]["option"]
        files = {}
        for key in ("groups", "rules"):
            uid = option.get(key)
            if not uid or not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
                raise ProfileError("当前订阅没有“扩展%s”文件；请在 Clash Verge 里升级到 2.0 以上后重试" % (
                    "分组" if key == "groups" else "规则"))
            path = self.profile_dir / (uid + ".yaml")
            if not path.is_file() or path.is_symlink():
                raise ProfileError("扩展文件不存在：%s" % path.name)
            files[key] = path
        return current, files["groups"], files["rules"]


# ---------------------------------------------------------------- YAML sequence editing
_ITEM_NAME = re.compile(r'^-\s+(?:\{\s*"name"\s*:\s*"((?:[^"\\]|\\.)*)"|name:\s*(.+?)\s*$)')


def _item_name(line):
    match = _ITEM_NAME.match(line.strip())
    if not match:
        return None
    if match.group(1) is not None:
        return json.loads('"%s"' % match.group(1))
    return _scalar(match.group(2))


def _find_key(lines, key):
    for index, line in enumerate(lines):
        if line.startswith(key + ":"):
            return index
    return None


def _sequence(lines, start):
    """(item indent, end index) of the block sequence under the top-level key at `start`."""
    indent = None
    end = start + 1
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            end = index + 1
            continue
        depth = len(line) - len(line.lstrip(" "))
        if depth == 0 and not line.lstrip().startswith("- ") and not line.lstrip().startswith("#"):
            break
        if depth == 0 and line.startswith("#"):
            break
        if indent is None and line.lstrip().startswith("- "):
            indent = depth
        end = index + 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return indent, end


def _split_items(lines, start, end, indent):
    """[(first, last_exclusive)] of each item of the sequence between start and end."""
    spans = []
    index = start
    while index < end:
        line = lines[index]
        depth = len(line) - len(line.lstrip(" "))
        if line.strip().startswith("- ") and depth == indent:
            stop = index + 1
            while stop < end:
                nxt = lines[stop]
                nd = len(nxt) - len(nxt.lstrip(" "))
                if nxt.strip() and (nd < indent or (nd == indent and not nxt.strip().startswith("#"))):
                    break
                if nxt.strip().startswith("#") and nd <= indent:
                    break
                stop += 1
            spans.append((index, stop))
            index = stop
        else:
            index += 1
    return spans


def strip_block(text):
    """Remove SteadyRoute's marked block (if any)."""
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skipping = True
            continue
        if skipping and line.strip() == MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def edit_prepend(text, items, remove_names=()):
    """Put `items` (YAML one-liners) in the marked block at the top of `prepend`.

    Unmarked prepend items whose `name` is in remove_names (an older SteadyRoute's groups) are
    taken out and returned, so they can be put back when the block is removed.
    Returns (new text, removed item texts).
    """
    lines = strip_block(text).splitlines()
    at = _find_key(lines, "prepend")
    if at is None:
        lines = ["prepend:"] + lines
        at = 0
    head = lines[at].partition(":")[2].strip()
    if head not in ("", "[]"):
        raise ProfileError("扩展文件的 prepend 是单行写法（%s），请在 Clash Verge 里改成多行后重试" % head[:40])
    lines[at] = "prepend:"
    indent, end = _sequence(lines, at)
    indent = 2 if indent is None else indent
    removed = []
    if remove_names:
        keep = []
        cursor = at + 1
        for first, last in _split_items(lines, at + 1, end, indent):
            keep.extend(lines[cursor:first])
            if _item_name(lines[first]) in remove_names:
                removed.append("\n".join(lines[first:last]))
            else:
                keep.extend(lines[first:last])
            cursor = last
        keep.extend(lines[cursor:end])
        lines = lines[:at + 1] + keep + lines[end:]
    if items:
        pad = " " * indent
        block = [pad + MARK_BEGIN] + [pad + "- " + item for item in items] + [pad + MARK_END]
        lines = lines[:at + 1] + block + lines[at + 1:]
    return "\n".join(lines) + "\n", removed


def restore_prepend(text, removed):
    """Remove the marked block and put back items taken out by edit_prepend."""
    lines = strip_block(text).splitlines()
    at = _find_key(lines, "prepend")
    if at is None:
        return "\n".join(lines) + "\n"
    if removed:
        lines[at] = "prepend:"
        restored = []
        for item in removed:
            restored.extend(item.splitlines())
        lines = lines[:at + 1] + restored + lines[at + 1:]
    else:
        indent, end = _sequence(lines, at)
        if indent is None:
            lines[at] = "prepend: []"
    return "\n".join(lines) + "\n"


def patch_runtime(text, group_items, rule_items, group_names, stale_rules=()):
    """Clash Verge's runtime config with our groups and rules at the top of their lists.

    Existing groups named in group_names (ours from before, or an older SteadyRoute's) are
    replaced; rule lines equal to one of ours (or stale_rules) are not duplicated.
    """
    lines = text.splitlines()
    ours = {_normal_rule(item) for item in rule_items} | {_normal_rule(item) for item in stale_rules}

    def rewrite(key, fresh, drop):
        nonlocal lines
        at = _find_key(lines, key)
        if at is None:
            raise ProfileError("运行配置里没有 %s" % key)
        head = lines[at].partition(":")[2].strip()
        if head == "[]":
            lines[at] = key + ":"
        elif head:
            raise ProfileError("运行配置的 %s 是单行写法，无法安全修改" % key)
        indent, end = _sequence(lines, at)
        indent = 0 if indent is None else indent
        body = []
        cursor = at + 1
        for first, last in _split_items(lines, at + 1, end, indent):
            body.extend(lines[cursor:first])
            if not drop(lines[first:last]):
                body.extend(lines[first:last])
            cursor = last
        body.extend(lines[cursor:end])
        pad = " " * indent
        lines = lines[:at + 1] + [pad + "- " + item for item in fresh] + body + lines[end:]

    rewrite("proxy-groups", group_items, lambda block: _item_name(block[0]) in group_names)
    rewrite("rules", rule_items, lambda block: len(block) == 1 and _normal_rule(block[0].strip()[2:]) in ours)
    return "\n".join(lines) + "\n"


def _normal_rule(item):
    item = item.strip()
    if item.startswith('"') and item.endswith('"'):
        try:
            item = json.loads(item)
        except ValueError:
            item = item[1:-1]
    elif item.startswith("'") and item.endswith("'"):
        item = item[1:-1]
    return ",".join(part.strip() for part in item.split(","))


def group_item(group):
    return json.dumps(group, ensure_ascii=False)


def rule_item(line):
    return json.dumps(line, ensure_ascii=False)


# ---------------------------------------------------------------- validation and apply
def core_validator(core, home):
    def validate(text):
        with tempfile.TemporaryDirectory(prefix="steadyroute-check-") as directory:
            staged = pathlib.Path(directory) / "staged.yaml"
            staged.write_text(text, encoding="utf-8")
            result = subprocess.run([core, "-d", str(home), "-f", str(staged), "-t"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode != 0:
            last = output.strip().splitlines()[-1] if output.strip() else "exit %d" % result.returncode
            raise ProfileError("Clash 内核校验没有通过：%s" % last[:300])
        return output
    return validate


def _atomic_write(path, text):
    path = pathlib.Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Writer(object):
    """Apply or remove SteadyRoute's block, with backup, core validation, reload and rollback.

    controller(method, path, payload) -> (status, body) talks to Mihomo; validate(text) raises
    ProfileError when the core rejects a config.
    """

    def __init__(self, target, backup_root, validate, controller):
        self.target = target
        self.backup_root = pathlib.Path(backup_root)
        self.validate = validate
        self.controller = controller

    def _backup(self, files, meta):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.backup_root / stamp
        folder.mkdir(parents=True, exist_ok=True)
        for label, path in files.items():
            shutil.copy2(str(path), str(folder / (label + ".yaml")))
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        backups = sorted(p for p in self.backup_root.iterdir() if p.is_dir())
        for old in backups[:-10]:
            shutil.rmtree(str(old), ignore_errors=True)
        return folder

    def _reload(self):
        status, body = self.controller("PUT", "/configs?force=true", {"path": str(self.target.runtime)})
        if status not in (200, 204):
            raise ProfileError("Clash 没有接受新配置（HTTP %s）：%s" % (status, body[:200]))

    def _selections(self, names):
        status, body = self.controller("GET", "/proxies", None)
        if status != 200:
            return {}
        proxies = (json.loads(body or "{}").get("proxies") or {})
        return {name: (proxies.get(name) or {}).get("now") for name in names if proxies.get(name)}

    def _reselect(self, selections):
        from urllib.parse import quote
        for group, node in selections.items():
            if node:
                self.controller("PUT", "/proxies/" + quote(group, safe=""), {"name": node})

    def commit(self, groups_text, rules_text, runtime_text, meta, verify=None, keep_selection=()):
        """Validate, back up, write the three files, reload, verify; roll everything back on failure."""
        _uid, groups_file, rules_file = self.target.resolve()
        self.validate(runtime_text)
        files = {"groups": groups_file, "rules": rules_file, "runtime": self.target.runtime}
        originals = {label: path.read_text(encoding="utf-8") for label, path in files.items()}
        backup = self._backup(files, meta)
        selections = self._selections(keep_selection)
        try:
            _atomic_write(groups_file, groups_text)
            _atomic_write(rules_file, rules_text)
            _atomic_write(self.target.runtime, runtime_text)
            self._reload()
            if verify:
                verify()
        except Exception as error:
            for label, path in files.items():
                _atomic_write(path, originals[label])
            try:
                self._reload()
            except Exception as reload_error:
                raise ProfileError("写入后检查失败（%s），文件已恢复，但重新加载原配置也失败（%s）；"
                                   "请在 Clash Verge 里重新选中当前订阅" % (error, reload_error))
            raise ProfileError("写入后检查失败，已恢复原配置：%s" % error)
        self._reselect({k: v for k, v in selections.items() if v})
        return backup
