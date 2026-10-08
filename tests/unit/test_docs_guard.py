"""docs-guard — the retired-claims and docs-impact checks every HLE repo runs.

The tool lives in .github/actions/docs-guard/ (it's a composite action other
repos use), so it is loaded by path rather than imported from the package.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parents[2] / ".github" / "actions" / "docs-guard"
_spec = importlib.util.spec_from_file_location("docs_guard", ACTION / "docs_guard.py")
assert _spec and _spec.loader
dg = importlib.util.module_from_spec(_spec)
sys.modules["docs_guard"] = dg
_spec.loader.exec_module(dg)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    # Under a git hook (this repo's pre-commit runs the suite), git exports
    # GIT_INDEX_FILE / GIT_DIR, which would point the scratch repos below at
    # the real repository's index.
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(var, raising=False)


# CI's test container (python:3.x-slim) has no git; the Docs Guard job, which
# runs the tool for real, uses an image that does.
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path: Path, config: str, files: dict[str, str], *, git: bool = True) -> Path:
    """A scratch repo. ``git=False`` when only the config is needed."""
    (tmp_path / "docs-guard.toml").write_text(config)
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if git:
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


CLAIMS = '[claims]\ninclude = ["docs/**/*.md", "README.md"]\n'


class TestGlobs:
    @pytest.mark.parametrize(
        ("glob", "path", "hit"),
        [
            ("docs/**/*.md", "docs/a.md", True),
            ("docs/**/*.md", "docs/x/y/a.md", True),
            ("docs/**/*.md", "docs/a.txt", False),
            ("*.md", "docs/a.md", False),
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/x/a.py", False),
            ("**", "anything/at/all", True),
        ],
    )
    def test_match(self, glob, path, hit):
        assert dg.matches(path, [glob]) is hit


class TestSharedRetiredClaims:
    """The shared list catches what it should, and leaves the current spellings alone."""

    @pytest.fixture
    def rules(self, tmp_path):
        return dg.load_config(_repo(tmp_path, CLAIMS, {}, git=False)).rules

    @pytest.mark.parametrize(
        "line",
        [
            "hle agent enroll hlea_xxxxx",
            "Open Agents → New Agent in the dashboard",
            "Dashboard -> Agents -> New Agent",
            "Connections → New → Agent",
            "hle expose --service http://localhost:8123 --label ha",
            "hle --debug expose --service http://x",
            "sudo hle service install --agent",
            "hle fp --agent box --to 22",
            "hle config access add ha you@example.com",
        ],
    )
    def test_flags_retired(self, rules, line):
        assert any(r.pattern.search(line) for r in rules), line

    @pytest.mark.parametrize(
        "line",
        [
            "hle agent enroll hle_0123456789abcdef0123456789abcdef",
            "Open Connections → Agents → New",
            "hle tunnel create ha http://localhost:8123",
            "hle daemon install agent --system",
            "hle forward create box 22",
            "Saved to ~/.config/hle/config.toml",
            "The hle_tunnel_token cookie",
        ],
    )
    def test_leaves_current_text_alone(self, rules, line):
        assert not any(r.pattern.search(line) for r in rules), line


class TestClaims:
    def test_finds_a_retired_claim_with_its_line(self, tmp_path):
        root = _repo(tmp_path, CLAIMS, {"docs/a.md": "ok\nuse hlea_123\n"})
        [finding] = dg.scan_claims(root, dg.load_config(root))
        assert (finding.path, finding.line, finding.rule.id) == (
            "docs/a.md",
            2,
            "hlea-agent-token",
        )

    def test_files_outside_include_are_not_scanned(self, tmp_path):
        root = _repo(tmp_path, CLAIMS, {"src/a.py": "hlea_ legacy\n"})
        assert dg.scan_claims(root, dg.load_config(root)) == []

    def test_inline_allow_marker(self, tmp_path):
        root = _repo(
            tmp_path,
            CLAIMS,
            {"docs/a.md": "Old hlea_ still works. <!-- docs-guard: allow hlea-agent-token -->\n"},
        )
        assert dg.scan_claims(root, dg.load_config(root)) == []

    def test_allow_marker_only_covers_the_rule_it_names(self, tmp_path):
        root = _repo(
            tmp_path,
            CLAIMS,
            {"docs/a.md": "hlea_ via hle expose  # docs-guard: allow hlea-agent-token\n"},
        )
        [finding] = dg.scan_claims(root, dg.load_config(root))
        assert finding.rule.id == "cli-pre-noun-verb"

    def test_file_level_allow_and_local_rules(self, tmp_path):
        config = CLAIMS + (
            '[[claims.allow]]\nrule = "hlea-agent-token"\nfiles = ["docs/legacy.md"]\n'
            '[[retired]]\nid = "old-port"\npattern = "port 8099"\nreason = "moved"\n'
        )
        root = _repo(
            tmp_path,
            config,
            {"docs/legacy.md": "hlea_ history\n", "README.md": "listens on port 8099\n"},
        )
        [finding] = dg.scan_claims(root, dg.load_config(root))
        assert (finding.path, finding.rule.id) == ("README.md", "old-port")

    def test_disable_a_shared_rule(self, tmp_path):
        config = '[claims]\ninclude = ["*.md"]\ndisable = ["hlea-agent-token"]\n'
        root = _repo(tmp_path, config, {"a.md": "hlea_\n"})
        assert dg.scan_claims(root, dg.load_config(root)) == []


IMPACT = (
    "[claims]\ninclude = []\n"
    '[[impact]]\nname = "CLI"\npaths = ["src/cli.py"]\ndocs = ["README.md", "docs/**"]\n'
)


class TestImpact:
    @pytest.fixture
    def cfg(self, tmp_path):
        return dg.load_config(_repo(tmp_path, IMPACT, {}, git=False))

    def test_code_without_docs_is_a_gap(self, cfg):
        [gap] = dg.find_gaps(cfg, ["src/cli.py", "tests/test_cli.py"])
        assert gap.code == ["src/cli.py"]

    def test_code_with_docs_is_fine(self, cfg):
        assert dg.find_gaps(cfg, ["src/cli.py", "docs/cli/ref.md"]) == []

    def test_unrelated_change_is_fine(self, cfg):
        assert dg.find_gaps(cfg, ["src/other.py"]) == []

    def test_gap_fails(self, cfg, capsys):
        assert dg.cmd_impact(Path("."), cfg, ["src/cli.py"], "", warn=False) == 1
        assert "Docs-Impact:" in capsys.readouterr().out

    def test_gap_only_warns_in_warn_mode(self, cfg):
        assert dg.cmd_impact(Path("."), cfg, ["src/cli.py"], "", warn=True) == 0

    @pytest.mark.parametrize(
        "message",
        [
            "Summary\n\nDocs-Impact: hle-world/hle#12\n",
            "docs-impact: none — internal refactor, no behaviour change",
        ],
    )
    def test_docs_impact_line_accepts_the_gap(self, cfg, message):
        assert dg.cmd_impact(Path("."), cfg, ["src/cli.py"], message, warn=False) == 0

    def test_an_empty_docs_impact_line_does_not_count(self, cfg):
        assert dg.cmd_impact(Path("."), cfg, ["src/cli.py"], "Docs-Impact:\n", warn=False) == 1

    def test_staged_end_to_end(self, tmp_path):
        root = _repo(tmp_path, IMPACT, {"src/cli.py": "x = 1\n"})
        assert dg.main(["--root", str(root), "impact", "--staged"]) == 1
        (root / "README.md").write_text("documented\n")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        assert dg.main(["--root", str(root), "impact", "--staged"]) == 0


@needs_git
def test_this_repo_passes_its_own_claims_check():
    root = Path(__file__).resolve().parents[2]
    assert dg.main(["--root", str(root), "claims"]) == 0
