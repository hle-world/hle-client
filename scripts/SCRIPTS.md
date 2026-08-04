# Scripts

| Script | What it does | Options |
|--------|--------------|---------|
| `scripts/release.sh` | Bump the version everywhere, add a CHANGELOG entry, open a release PR. Merging the PR creates the GitHub release, which publishes to PyPI and updates Homebrew. | `<version>` `--dry-run` `--tag` |
| `scripts/capture_wire_baseline.py` | Snapshot the JSON every wire model produces, for protocol compatibility checking. The output is the fixture `tests/unit/test_wire_compat.py` asserts against. | `-o/--output <path>` |
| `scripts/pre-commit` | Git hook: lint and format checks before a commit. | — |

## Regenerating the wire baseline

`tests/fixtures/wire_baseline.json` is the wire contract with every deployed
client and with the relay. Regenerate it only when you *intend* to change the
protocol, never to make a failing test pass:

```sh
./scripts/capture_wire_baseline.py -o tests/fixtures/wire_baseline.json
```

A diff in that file is a protocol change. Review it as one.
