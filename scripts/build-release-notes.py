#!/usr/bin/env python3
"""Validate the human release ledger and export a local read-only JSON view."""

import argparse
import hashlib
import json
import pathlib
import re
import sys


PROJECT_DIR = pathlib.Path(__file__).resolve().parents[1]
SOURCE = PROJECT_DIR / "docs" / "VERSION_HISTORY.md"
VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
HEADING = re.compile(r"^## (v\d+\.\d+\.\d+) — (.+)$")
FIELD = re.compile(r"^- \*\*(关联|实际改变|测试与生产验收|已知限制|备份与回滚)：\*\*\s*(.+)$")
REQUIRED = ("关联", "实际改变", "测试与生产验收", "已知限制", "备份与回滚")


class ReleaseNotesError(ValueError):
    pass


def plain(value):
    value = re.sub(r"\[([^]]+)\]\(https?://[^)]+\)", r"\1", value)
    return value.replace("**", "").replace("`", "").strip()


def parse_release_history(source=SOURCE):
    source_bytes = pathlib.Path(source).read_bytes()
    content = source_bytes.decode("utf-8")
    overview = {}
    for line in content.splitlines():
        if not line.startswith("| v"):
            continue
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not VERSION.fullmatch(cells[0]):
            raise ReleaseNotesError("invalid version overview row")
        version, date, _code, status = cells
        if version in overview or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not status:
            raise ReleaseNotesError("duplicate or incomplete version overview: %s" % version)
        overview[version] = {"date": date, "release_status": plain(status)}

    sections = {}
    current = None
    active_field = None
    for line in content.splitlines():
        heading = HEADING.match(line)
        if heading:
            version, title = heading.groups()
            if version in sections:
                raise ReleaseNotesError("duplicate version section: %s" % version)
            current = {"version": version, "title": title, "fields": {}}
            sections[version] = current
            active_field = None
            continue
        if current is None:
            continue
        field = FIELD.match(line)
        if field:
            key, value = field.groups()
            if key in current["fields"]:
                raise ReleaseNotesError("duplicate %s in %s" % (key, current["version"]))
            current["fields"][key] = value
            active_field = key
        elif line.startswith("## "):
            raise ReleaseNotesError("unrecognized version heading: %s" % line)
        elif active_field and line.strip() and not line.startswith("- "):
            current["fields"][active_field] += " " + line.strip()
        elif not line.strip():
            active_field = None

    if not overview or set(overview) != set(sections):
        raise ReleaseNotesError("version overview and detail sections must match exactly")
    releases = []
    for version, section in sections.items():
        missing = set(REQUIRED) - set(section["fields"])
        if missing:
            raise ReleaseNotesError("missing fields for %s: %s" % (version, ", ".join(sorted(missing))))
        releases.append({
            "version": version,
            "date": overview[version]["date"],
            "title": plain(section["title"]),
            "release_status": overview[version]["release_status"],
            "references": plain(section["fields"]["关联"]),
            "actual_changes": plain(section["fields"]["实际改变"]),
            "verification": plain(section["fields"]["测试与生产验收"]),
            "limitations": plain(section["fields"]["已知限制"]),
            "rollback": plain(section["fields"]["备份与回滚"]),
        })
    releases.sort(key=lambda item: tuple(map(int, VERSION.fullmatch(item["version"]).groups())), reverse=True)
    if any(not value for item in releases for value in item.values()):
        raise ReleaseNotesError("release notes contain an empty field")
    return {
        "schema_version": 1,
        "generated_from": "docs/VERSION_HISTORY.md",
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "releases": releases,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate without creating a file")
    parser.add_argument("--output", type=pathlib.Path, help="write packaged JSON")
    args = parser.parse_args(argv)
    if not args.check and args.output is None:
        parser.error("use --check or --output")
    try:
        payload = parse_release_history()
    except (OSError, ReleaseNotesError) as error:
        print("release notes invalid: %s" % error, file=sys.stderr)
        return 1
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("release notes: %d versions validated" % len(payload["releases"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
