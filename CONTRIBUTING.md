# Contributing

Thanks for helping improve HLE Client.

## Development

```bash
git clone https://github.com/hle-world/hle-client.git
cd hle-client
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

pytest
ruff check src/ tests/
ruff format --check src/ tests/
```

## Public repo hygiene

This repository is public. PR bodies, issues, commits and files must not
include:

- links to or names of private repositories
- server route or auth internals
- LAN IP addresses or MAC addresses
- session URLs
- personal hostnames

Write "tracked privately" for private references. `scripts/check-public-hygiene.sh`
enforces this in CI (the `public-hygiene` job).
