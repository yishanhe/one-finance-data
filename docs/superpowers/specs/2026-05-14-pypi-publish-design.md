# Design: Publish `onefinance` to PyPI

**Date:** 2026-05-14  
**Scope:** One-time manual publish to pypi.org using `uv publish`.

## Goal

Make `onefinance` installable via:

```bash
pip install onefinance
uv add onefinance
uv add "onefinance[cli]"
```

## Name availability

- `onefinance` — available (404 on pypi.org as of 2026-05-14)
- `one-finance-data` and `one-finance` also available; stick with `onefinance` (matches the Python import name)

## Changes to pyproject.toml

Three additions make the PyPI listing complete and searchable:

### 1. Author email

```toml
authors = [
    { name = "Shanhe Yi", email = "ysh@yishanhe.net" },
]
```

### 2. Project URLs

```toml
[project.urls]
Repository = "https://github.com/yishanhe/one-finance-data"
```

### 3. Classifiers

```toml
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Intended Audience :: Financial and Insurance Industry",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Office/Business :: Financial",
    "Topic :: Software Development :: Libraries :: Python Modules",
]
```

## Publish steps (manual, one-time)

1. **User creates a PyPI account** at https://pypi.org/account/register/ (if not already done)
2. **User creates an API token** at https://pypi.org/manage/account/token/ — scope: "Entire account" for the first upload (after first upload, scope it to the `onefinance` project)
3. **Build**: `uv build`
4. **Publish**: `uv publish --token pypi-<your-token>`

## Out of scope

- TestPyPI dry-run
- GitHub Actions automated publish
- Versioning automation / CHANGELOG
- Trusted Publisher (OIDC) setup
