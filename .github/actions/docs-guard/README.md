# docs-guard

Keeps HLE's docs from drifting away from the code. Every HLE repo runs it from
here:

```yaml
jobs:
  docs-guard:
    runs-on: <your runner>
    container: { image: "python:3.12" }   # python3 3.11+ and git
    steps:
      - uses: actions/checkout@v4
      - uses: hle-world/hle-client/.github/actions/docs-guard@main
```

Trigger it on `pull_request` with `types: [opened, synchronize, reopened, edited]`,
so that adding a `Docs-Impact:` line to the PR description re-runs it.

## The two checks

**Retired claims.** These are facts that used to be true and must not come back
into the docs: the `hlea_` agent token, old dashboard paths, and the old CLI
spellings. The shared list is [`retired.toml`](retired.toml). When a change
retires something users can see, add its entry in the same PR, and every repo
is checked against it from then on. If a line has to mention a retired claim on
purpose, mark it with `docs-guard: allow <rule-id>`, in a comment if the file
format needs one.

**Docs impact.** Each `[[impact]]` rule in a repo's `docs-guard.toml` maps code
paths to the docs that describe them. A PR that changes the code without those
docs fails until it either updates them or says why not in its description:

```
Docs-Impact: <link to the docs PR in another repo>
Docs-Impact: none — internal refactor, nothing user-visible
```

## docs-guard.toml

```toml
[claims]
include = ["README.md", "docs/**/*.md"]   # files scanned for retired claims
exclude = []
disable = []                               # shared rule ids that don't apply here

[[claims.allow]]                           # whole-file exemption
rule = "hlea-agent-token"
files = ["docs/legacy.md"]

[[retired]]                                # repo-local retired claims
id = "old-port"
pattern = 'port 8099'
reason = "The web UI moved to 8100 in v2611.1."
use = "8100"

[[impact]]
name = "CLI commands and options"
paths = ["src/cli.py"]
docs = ["README.md", "docs/**"]
note = "Shown with the failure."
```

## Locally

`scripts/install-git-hooks.sh` in the orchestrator repo installs a `commit-msg`
hook that runs both checks on staged files. Locally it only warns. CI is what
blocks. A `Docs-Impact:` line in the commit message silences the impact warning.

```bash
python3 .github/actions/docs-guard/docs_guard.py claims
python3 .github/actions/docs-guard/docs_guard.py impact --base origin/main
```
