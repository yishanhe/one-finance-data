# Design: Make `onefinance` installable from GitHub

**Date:** 2026-05-14  
**Scope:** Minimal packaging hygiene so `uv add git+<url>` works cleanly.

## Goal

Users should be able to install `onefinance` directly from GitHub without a PyPI publish:

```bash
uv add git+https://github.com/yishanhe/one-finance-data          # core
uv add "git+https://github.com/yishanhe/one-finance-data[cli]"   # core + CLI
```

## Current state

- `uv build` already produces a clean wheel (only `onefinance/` package files).
- The sdist bundles `.claude/settings.local.json` (sensitive), `CLAUDE.md`, `TODO.md`, `scripts/scratch*.py`, and internal `docs/`.
- `pyproject.toml` declares `dev` in both `[project.optional-dependencies]` and `[dependency-groups]` — redundant and confusing.
- `dist/` is not in `.gitignore` — built artifacts could be accidentally committed.
- 12 commits are unpushed to `origin`.

## Changes

### 1. `pyproject.toml` — sdist excludes

Add `[tool.hatch.build.targets.sdist]` with an exclude list:

```toml
[tool.hatch.build.targets.sdist]
exclude = [".claude/", "CLAUDE.md", "TODO.md", "scripts/", "docs/"]
```

### 2. `pyproject.toml` — drop duplicate `dev` optional

Remove `dev` from `[project.optional-dependencies]` (keep it only in `[dependency-groups]`). `cli` and `dev` are the only relevant extras; `dev` as a pip-installable extra is misleading.

### 3. `.gitignore` — add `dist/`

Append `/dist/` so built artifacts are never accidentally committed.

### 4. Push to GitHub

After committing the above changes, push `main` to `origin`.

## Out of scope

- PyPI publish
- GitHub Actions CI
- Versioned git tags
- `CHANGELOG.md`
