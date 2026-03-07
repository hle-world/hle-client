# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in hle-client, please report it responsibly.

**Email:** security@hle.world

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

We will acknowledge your report within 48 hours and aim to provide a fix within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |
| < Latest | No       |

## Security Measures

- Automated dependency auditing via `pip-audit`
- Static analysis with Bandit
- Secret scanning with TruffleHog
- All releases are tested before publishing to PyPI
