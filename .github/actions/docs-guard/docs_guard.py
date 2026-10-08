#!/usr/bin/env python3
"""docs-guard: keep HLE's docs from drifting away from the code.

Two checks, both driven by a ``docs-guard.toml`` at the root of the repo
being checked:

``claims``
    Scan documentation for *retired claims* — facts that used to be true and
    must not come back (an old token prefix, an old dashboard path, an old
    CLI spelling). The shared list ships next to this script in
    ``retired.toml``; a repo can add its own ``[[retired]]`` entries. A line
    that has to mention a retired claim on purpose (a "legacy tokens still
    work" note) carries ``docs-guard: allow <rule-id>`` anywhere on it, or the
    file is listed under ``[[claims.allow]]``.

``impact``
    Given the files a change touches, find ``[[impact]]`` rules whose code
    paths changed while none of their doc paths did. The change is accepted
    anyway when its PR body or commit message has a ``Docs-Impact:`` line
    saying where the docs went (another repo's PR) or why none are needed.

Standard library only (3.11+), so any repo's CI can run it without installing
anything.

Usage::

    docs_guard.py claims [--files F ...]
    docs_guard.py impact (--base REF | --staged) [--message-file F] [--warn]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED_RETIRED = HERE / "retired.toml"
CONFIG_NAME = "docs-guard.toml"
# The line may be a list item or bold (`- **Docs-Impact:** none`) — that is how
# people write it in a PR description. Horizontal whitespace only: `\s` would
# let an empty "Docs-Impact:" borrow the next line as its reason.
ESCAPE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*|__)?Docs-Impact:(?:\*\*|__)?[ \t]*(\S.*)$",
    re.MULTILINE | re.IGNORECASE,
)
ALLOW_MARK = re.compile(r"docs-guard:\s*allow\s+([\w,\s-]+)")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob with ``**`` support into an anchored regex."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, globs: list[str]) -> bool:
    return any(glob_to_regex(g).match(path) for g in globs)


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    reason: str
    use: str


@dataclass
class Config:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    allow: dict[str, list[str]] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    impact: list[dict[str, object]] = field(default_factory=list)


def _rules(entries: list[dict[str, str]]) -> list[Rule]:
    return [
        Rule(
            id=e["id"],
            pattern=re.compile(e["pattern"]),
            reason=e["reason"],
            use=e.get("use", ""),
        )
        for e in entries
    ]


def load_config(root: Path) -> Config:
    path = root / CONFIG_NAME
    if not path.exists():
        sys.exit(f"docs-guard: no {CONFIG_NAME} in {root}")
    data = tomllib.loads(path.read_text())
    shared = tomllib.loads(SHARED_RETIRED.read_text()).get("retired", [])
    claims = data.get("claims", {})
    allow: dict[str, list[str]] = {}
    for entry in claims.get("allow", []):
        allow.setdefault(entry["rule"], []).extend(entry["files"])
    disabled = set(claims.get("disable", []))
    rules = [r for r in _rules(shared + data.get("retired", [])) if r.id not in disabled]
    return Config(
        include=claims.get("include", []),
        exclude=claims.get("exclude", []),
        allow=allow,
        rules=rules,
        impact=data.get("impact", []),
    )


def git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: Rule
    text: str


def scan_claims(root: Path, cfg: Config, files: list[str] | None = None) -> list[Finding]:
    candidates = files if files is not None else git(root, "ls-files")
    findings: list[Finding] = []
    for rel in candidates:
        if not matches(rel, cfg.include) or matches(rel, cfg.exclude):
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, text in enumerate(lines, start=1):
            allowed: set[str] = set()
            if mark := ALLOW_MARK.search(text):
                allowed = {a.strip() for a in re.split(r"[,\s]+", mark.group(1)) if a.strip()}
            for rule in cfg.rules:
                if rule.id in allowed or matches(rel, cfg.allow.get(rule.id, [])):
                    continue
                if rule.pattern.search(text):
                    findings.append(Finding(rel, number, rule, text.strip()))
    return findings


@dataclass(frozen=True)
class Gap:
    name: str
    code: list[str]
    docs: list[str]
    note: str


def find_gaps(cfg: Config, changed: list[str]) -> list[Gap]:
    gaps: list[Gap] = []
    for rule in cfg.impact:
        paths = list(rule.get("paths", []))  # type: ignore[call-overload]
        docs = list(rule.get("docs", []))  # type: ignore[call-overload]
        touched = [f for f in changed if matches(f, paths)]
        if touched and not any(matches(f, docs) for f in changed):
            gaps.append(Gap(str(rule["name"]), touched, docs, str(rule.get("note", ""))))
    return gaps


def _annotate(level: str, msg: str, path: str | None = None, line: int | None = None) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    where = ""
    if path:
        where = f" file={path}" + (f",line={line}" if line else "")
    print(f"::{level}{where}::{msg.replace(chr(10), '%0A')}")


def cmd_claims(root: Path, cfg: Config, files: list[str] | None) -> int:
    findings = scan_claims(root, cfg, files)
    for f in findings:
        msg = f"[{f.rule.id}] {f.rule.reason}" + (f" Use: {f.rule.use}" if f.rule.use else "")
        print(f"{f.path}:{f.line}: {msg}\n    {f.text}")
        _annotate("error", msg, f.path, f.line)
    if findings:
        print(
            f"\ndocs-guard: {len(findings)} retired claim(s). Fix them, or mark a line that "
            "must mention one on purpose with `docs-guard: allow <rule-id>`."
        )
        return 1
    print(f"docs-guard: no retired claims ({len(cfg.rules)} rules).")
    return 0


def cmd_impact(root: Path, cfg: Config, changed: list[str], message: str, warn: bool) -> int:
    gaps = find_gaps(cfg, changed)
    if not gaps:
        print("docs-guard: docs impact OK.")
        return 0
    escape = ESCAPE.search(message)
    level = "warning" if (warn or escape) else "error"
    for gap in gaps:
        msg = f"{gap.name}: changed {', '.join(gap.code)} but none of {', '.join(gap.docs)}." + (
            f" {gap.note}" if gap.note else ""
        )
        print(f"{level}: {msg}")
        _annotate(level, msg, gap.code[0])
    if escape:
        print(f"docs-guard: accepted — Docs-Impact: {escape.group(1).strip()}")
        return 0
    print(
        "\ndocs-guard: update the docs above, or add a line to the PR description "
        "(or commit message):\n"
        "    Docs-Impact: <link to the docs PR>   or   Docs-Impact: none — <why>"
    )
    return 0 if warn else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docs-guard", description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="cmd", required=True)
    claims = sub.add_parser("claims", help="scan docs for retired claims")
    claims.add_argument("--files", nargs="*", help="only these files (default: all tracked)")
    impact = sub.add_parser("impact", help="code changed without its docs")
    src = impact.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="diff BASE..HEAD (CI)")
    src.add_argument("--staged", action="store_true", help="the staged files (git hooks)")
    impact.add_argument("--message-file", type=Path, help="PR body or commit message")
    impact.add_argument("--warn", action="store_true", help="report, but never fail")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    cfg = load_config(root)
    if args.cmd == "claims":
        return cmd_claims(root, cfg, args.files)

    if args.staged:
        changed = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    else:
        changed = git(root, "diff", "--name-only", args.base, "HEAD")
    message = ""
    if args.message_file and args.message_file.exists():
        message = args.message_file.read_text()
    message += "\n" + os.environ.get("DOCS_GUARD_MESSAGE", "")
    return cmd_impact(root, cfg, changed, message, args.warn)


if __name__ == "__main__":
    sys.exit(main())
