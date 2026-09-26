#!/usr/bin/env python3
"""Refuse to pass the check gate when a tracked or new file looks like it holds a secret.

Pure standard library, so it cannot be skipped because some tool is missing (the old
`rg` pipeline silently passed on machines without ripgrep). Files come from git, which
already knows what is ignored; Markdown and this scanner itself are skipped.

Exit codes: 0 clean, 1 findings, 2 the scan could not run.
"""

import pathlib
import re
import subprocess
import sys

PATTERN = re.compile(
    r"(subscription-url"
    r"|token\s*[:=]\s*\S+"
    r"|password\s*[:=]\s*\S+)"
)
SKIP_SUFFIXES = (".md",)
SKIP_FILES = {"scripts/check.sh", "scripts/leak-scan.py"}


def candidate_files(root):
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout
    for raw in output.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES):
            continue
        yield name


def scan(root, names):
    findings = []
    for name in names:
        path = pathlib.Path(root, name)
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue          # deleted in the working tree, or a submodule
        if b"\0" in data[:8192]:
            continue          # binary
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            match = PATTERN.search(line)
            if match:
                key = match.group(0).split(":")[0].split("=")[0].strip()
                findings.append("%s:%d: %s = <hidden>" % (name, number, key))
    return findings


def main(argv):
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".").resolve()
    try:
        names = list(candidate_files(root))
    except (OSError, subprocess.CalledProcessError) as error:
        print("secret scan could not list files (is this a git checkout?): %s" % error, file=sys.stderr)
        return 2
    findings = scan(root, names)
    if findings:
        print("Potential secret material detected; review before commit:", file=sys.stderr)
        for item in findings:
            print("  " + item, file=sys.stderr)
        return 1
    print("secret scan: %d files clean" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
