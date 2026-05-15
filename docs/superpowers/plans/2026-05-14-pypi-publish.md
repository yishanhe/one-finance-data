# PyPI Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `onefinance` to pypi.org so it's installable via `pip install onefinance` and `uv add onefinance`.

**Architecture:** Update `pyproject.toml` with complete PyPI metadata (author email, project URL, classifiers), build a clean sdist + wheel with `uv build`, then publish with `uv publish`. No new files — one file modified, one shell command run.

**Tech Stack:** hatchling (build backend), uv (build + publish), pypi.org

---

## Files

- Modify: `pyproject.toml` — add author email, `[project.urls]`, and `classifiers`

---

### Task 1: Complete pyproject.toml metadata and build

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add author email, project URL, and classifiers to pyproject.toml**

Replace the existing `authors` line and add `classifiers` + `[project.urls]`. The full updated `[project]` table should look like this (keep all existing fields, only add the marked lines):

```toml
[project]
name = "onefinance"
version = "0.1.0"
description = "One finance data client to rule them all — unified API across multiple financial data providers"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11"
authors = [
    { name = "Shanhe Yi", email = "ysh@yishanhe.net" },
]
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

Add this block immediately after `[project.optional-dependencies]` and before `[project.scripts]`:

```toml
[project.urls]
Repository = "https://github.com/yishanhe/one-finance-data"
```

- [ ] **Step 2: Run tests to confirm nothing broke**

```bash
uv run pytest tests/ -m "not integration" -q
```

Expected: `328 passed, 14 deselected`

- [ ] **Step 3: Build**

```bash
rm -rf dist && uv build
```

Expected:
```
Building source distribution...
Building wheel from source distribution...
Successfully built dist/onefinance-0.1.0.tar.gz
Successfully built dist/onefinance-0.1.0-py3-none-any.whl
```

- [ ] **Step 4: Verify metadata is embedded correctly in the wheel**

```bash
uv run python -c "
import zipfile, email
z = zipfile.ZipFile('dist/onefinance-0.1.0-py3-none-any.whl')
meta = z.read('onefinance-0.1.0.dist-info/METADATA').decode()
for line in meta.splitlines()[:30]:
    print(line)
"
```

Confirm the output contains:
- `Author-email: Shanhe Yi <ysh@yishanhe.net>`
- `Project-URL: Repository, https://github.com/yishanhe/one-finance-data`
- `Classifier: License :: OSI Approved :: MIT License`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add PyPI metadata (author email, URL, classifiers)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Create PyPI account + API token (manual user step)

**Files:** none — this is done in the browser.

⚠️ **This task requires action in the browser. Claude cannot do this for you.**

- [ ] **Step 1: Register or log in at pypi.org**

Go to https://pypi.org/account/register/ and create an account if you don't have one. Verify your email.

- [ ] **Step 2: Create an API token**

1. Go to https://pypi.org/manage/account/token/
2. Click **Add API token**
3. Token name: `onefinance-initial`
4. Scope: **Entire account** (required for a first upload; you can re-scope to the project afterward)
5. Click **Create token**
6. **Copy the token now** — it starts with `pypi-` and is shown only once.

---

### Task 3: Publish to PyPI

**Files:** none — this is a `uv publish` invocation.

- [ ] **Step 1: Push the metadata commit to GitHub**

```bash
git push origin main
```

Expected:
```
To git@github.com:yishanhe/one-finance-data.git
   <old>..<new>  main -> main
```

- [ ] **Step 2: Publish**

Replace `<your-token>` with the `pypi-...` token from Task 2:

```bash
uv publish --token <your-token>
```

Expected output:
```
Publishing onefinance 0.1.0 to https://upload.pypi.org/legacy/
Uploading onefinance-0.1.0.tar.gz
Uploading onefinance-0.1.0-py3-none-any.whl
```

If you see `File already exists` it means a previous attempt partially succeeded — that's fine, PyPI deduplicates by filename.

- [ ] **Step 3: Verify the package is live**

```bash
uv run --isolated --with onefinance python -c "import onefinance; print('pypi install: ok')"
```

Expected: `pypi install: ok` (uv will resolve from PyPI, not the local repo)

You can also visit https://pypi.org/project/onefinance/ in a browser to see the listing.

- [ ] **Step 4: (Optional) Narrow the API token scope**

After the first successful publish, go to https://pypi.org/manage/account/ and update the `onefinance-initial` token to be scoped to the `onefinance` project only. This limits blast radius if the token is ever leaked.
