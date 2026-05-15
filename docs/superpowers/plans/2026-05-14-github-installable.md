# GitHub-Installable Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `onefinance` installable via `uv add git+https://github.com/yishanhe/one-finance-data` with a clean sdist and no confusing duplicate extras.

**Architecture:** Two edits to `pyproject.toml` (sdist excludes + drop duplicate dev extra), then commit and push. No new files. No tests needed — verification is inspecting build output.

**Tech Stack:** hatchling (build backend), uv (build + push)

---

## Files

- Modify: `pyproject.toml` — add `[tool.hatch.build.targets.sdist]` excludes; remove `dev` from `[project.optional-dependencies]`

---

### Task 1: Add sdist excludes to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

The sdist currently bundles `.claude/settings.local.json`, `CLAUDE.md`, `TODO.md`, `scripts/`, and `docs/`. None of these are relevant to users installing the package.

- [ ] **Step 1: Edit pyproject.toml — add the sdist exclude section**

Add this block immediately after `[project.scripts]` and before `[dependency-groups]`:

```toml
[tool.hatch.build.targets.sdist]
exclude = [
    ".claude/",
    "CLAUDE.md",
    "TODO.md",
    "scripts/",
    "docs/",
]
```

The full relevant portion of `pyproject.toml` should look like:

```toml
[project.scripts]
ofclient = "onefinance.cli.app:app"

[tool.hatch.build.targets.sdist]
exclude = [
    ".claude/",
    "CLAUDE.md",
    "TODO.md",
    "scripts/",
    "docs/",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    ...
]
```

- [ ] **Step 2: Remove the duplicate `dev` entry from `[project.optional-dependencies]`**

Change:

```toml
[project.optional-dependencies]
cli = ["typer>=0.12", "rich"]
dev = ["pytest", "pytest-asyncio", "mypy", "ruff"]
```

To:

```toml
[project.optional-dependencies]
cli = ["typer>=0.12", "rich"]
```

`dev` belongs only in `[dependency-groups]` (the uv-native section). Keeping it in `[project.optional-dependencies]` would let someone accidentally do `uv add onefinance[dev]` and get the older pinned versions.

- [ ] **Step 3: Verify the build still works**

```bash
uv build
```

Expected output (exact version may differ):
```
Building source distribution...
Building wheel from source distribution...
Successfully built dist/onefinance-0.1.0.tar.gz
Successfully built dist/onefinance-0.1.0-py3-none-any.whl
```

- [ ] **Step 4: Inspect the sdist to confirm private files are excluded**

```bash
uv run python -c "
import tarfile
t = tarfile.open('dist/onefinance-0.1.0.tar.gz')
names = sorted(m.name for m in t.getmembers())
for n in names:
    print(n)
"
```

Confirm the output does NOT contain any of:
- `onefinance-0.1.0/.claude/`
- `onefinance-0.1.0/CLAUDE.md`
- `onefinance-0.1.0/TODO.md`
- `onefinance-0.1.0/scripts/`
- `onefinance-0.1.0/docs/`

Expected to still contain: `onefinance-0.1.0/pyproject.toml`, `onefinance-0.1.0/README.md`, `onefinance-0.1.0/LICENSE`, `onefinance-0.1.0/onefinance/...`

- [ ] **Step 5: Confirm the wheel is still clean**

```bash
uv run python -c "
import zipfile
z = zipfile.ZipFile('dist/onefinance-0.1.0-py3-none-any.whl')
for n in sorted(z.namelist()):
    print(n)
"
```

Expected: only `onefinance/` package files and `onefinance-0.1.0.dist-info/` metadata. No `tests/`, `scripts/`, `docs/`.

- [ ] **Step 6: Run unit tests to confirm nothing broke**

```bash
uv run pytest tests/ -m "not integration" -q
```

Expected: all tests pass (328 passed as of last run).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml
git commit -m "chore: clean sdist excludes and drop duplicate dev extra

- Exclude .claude/, CLAUDE.md, TODO.md, scripts/, docs/ from sdist
- Remove dev from project.optional-dependencies (kept in dependency-groups)
- dist/ was already in .gitignore

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Push to GitHub

**Files:** (none — git operation only)

- [ ] **Step 1: Verify you are on main and check what will be pushed**

```bash
git log origin/main..HEAD --oneline
```

Expected: a list of commits including the packaging commit from Task 1 and all prior unpushed work. Verify the list looks right before pushing.

- [ ] **Step 2: Push**

```bash
git push origin main
```

Expected:
```
To git@github.com:yishanhe/one-finance-data.git
   <old-sha>..<new-sha>  main -> main
```

- [ ] **Step 3: Verify the install URL works**

Test installing from the freshly pushed repo:

```bash
# In a throw-away temp venv
uv run --isolated --with "git+https://github.com/yishanhe/one-finance-data" python -c "import onefinance; print(onefinance.__version__ if hasattr(onefinance, '__version__') else 'ok')"
```

Expected: prints `ok` (or the version string if `__version__` is defined) without errors.

For the CLI extra:

```bash
uv run --isolated --with "git+https://github.com/yishanhe/one-finance-data[cli]" ofclient --help
```

Expected: prints the ofclient help text.
