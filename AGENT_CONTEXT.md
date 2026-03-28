# Nexus CLI — Agent Context & Skill File

> **Purpose**: This document is the single source of truth for any LLM agent (Cursor, Roo Code, GitHub Copilot, Claude, etc.) working on this codebase. Read this before writing any code.

---

## 1. What This Project Is

**Nexus CLI** is a **proprietary fork of [aider-chat](https://github.com/Aider-AI/aider)** transformed into an enterprise zero-config coding assistant. The internal package name remains `aider/` to minimize refactoring, but the CLI command is `nexus`.

### The Stack

```
Developer types: nexus
    │
    ▼
Nexus CLI (this repo — aider fork)
    │  POST /v1/chat/completions
    │  GET  /api/skills/<name>
    │  POST /api/overflow/ingest
    │  Header: X-Nexus-Skill: <skill>
    ▼
Nexus FastAPI Backend  (NOT in this repo)
    │  (backend handles all authentication via service account)
    │
    ├─► RAG pipeline (Confluence, internal docs)
    ├─► Skills DB (product-specific rules as markdown)
    └─► LLM (Google ADK / Claude / GPT)
```

### Key Constraints (Read These First)

1. **No credentials from CLI**: Authentication is handled server-side by the backend service account. The CLI sends no auth headers.
2. **Model is hardcoded**: `openai/nexus-agent` pointing to `http://localhost:8000/v1`. Do not change this to a real LLM.
3. **SEARCH/REPLACE format is sacred**: The LLM response MUST use aider's SEARCH/REPLACE block format or local file edits break. The backend must never modify `messages[0]` (aider's system prompt containing the format rules).
4. **Python 3.11+ required**: Due to numpy dependency split. Tested on Python 3.14.3 with `uv`.

---

## 2. Repository Layout

```
nexus-cli/
├── aider/                          # Core — original aider package (DO NOT rename)
│   ├── nexus_auth.py               # ← NEW: skill detection + config cache (no credentials)
│   ├── main.py                     # ← MODIFIED: hardwired endpoint + skill detection call
│   ├── models.py                   # ← MODIFIED: validation bypass + header injection
│   ├── commands.py                 # ← MODIFIED: /solve command added
│   └── coders/
│       └── base_coder.py           # ← MODIFIED: @skill interceptor added
├── docs/
│   ├── nexus-backend-openapi.yaml  # ← NEW: OpenAPI 3.1 spec (5 endpoints)
│   └── nexus-backend-integration-guide.md  # ← NEW: backend implementation guide
├── mock_backend.py                 # ← NEW: FastAPI mock for local testing
├── test_integration.py             # ← NEW: integration test suite
├── build_nexus.py                  # ← NEW: PyInstaller build script
├── pyproject.toml                  # ← MODIFIED: name=nexus-cli, entry=nexus
├── README.md                       # ← UPDATED: Nexus CLI docs (not aider)
├── README_NEXUS.md                 # ← NEW: user guide
├── BACKEND_CONTEXT.md              # ← NEW: deep backend implementation guide
├── INSTALLATION_NOTES.md           # ← NEW: install instructions
├── VERIFICATION_REPORT.md          # ← NEW: test results
└── DELIVERABLES.md                 # ← NEW: complete file inventory
```

---

## 3. Every Modified File — Exact Changes

### 3.1 `pyproject.toml`

Two changes only:

```toml
# BEFORE
name = "aider-chat"
[project.scripts]
aider = "aider.main:main"

# AFTER
name = "nexus-cli"
requires-python = ">=3.11"     # was >=3.9
[project.scripts]
nexus = "aider.main:main"      # command renamed; entry point unchanged
```

---

### 3.2 `aider/main.py` — Two Modification Blocks

**Block 1** — Hardwire LLM endpoint (~line 509):
```python
# --- Nexus CLI: hardwire endpoint, disable provider selection ---
os.environ["OPENAI_API_KEY"] = "nexus-passthrough"
os.environ["OPENAI_API_BASE"] = "http://localhost:8000/v1"
args.model = "openai/nexus-agent"
args.openai_api_key = "nexus-passthrough"
args.openai_api_base = "http://localhost:8000/v1"
# --- End Nexus hardwiring ---
```

**Block 2** — Skill detection + health check (~line 843):
```python
# --- Nexus CLI: wire skill detection ---
if "nexus-agent" in args.model:
    from aider.nexus_auth import detect_active_skill

    # Auth is handled server-side by the backend service account
    nexus_headers = {}

    try:
        active_skill = detect_active_skill(io, git_root)
        nexus_headers["X-Nexus-Skill"] = active_skill
        io.tool_output(f"Active product context: {active_skill}")
    except Exception as e:
        io.tool_warning(f"Skill detection failed: {e}")
        io.tool_warning("Continuing without product context.")

    main_model._nexus_extra_headers = nexus_headers

    try:
        import requests
        resp = requests.get("http://localhost:8000/v1/models", timeout=5)
        resp.raise_for_status()
    except Exception:
        io.tool_error("Cannot reach Nexus backend at http://localhost:8000")
        io.tool_error("Please ensure the Nexus backend is running.")
        return 1
# --- End Nexus CLI wiring ---
```

---

### 3.3 `aider/models.py` — Two Modifications

**Edit 1** — Validation bypass (~line 728):
```python
def validate_environment(self):
    # Nexus CLI: skip validation for nexus-agent model
    if "nexus-agent" in self.name:
        return dict(keys_in_environment=["OPENAI_API_KEY"], missing_keys=[])
    # ... rest of original validation ...
```

**Edit 2** — Header injection in `send_completion()` (~line 1020):
```python
# Nexus CLI: inject skill header if present
if hasattr(self, "_nexus_extra_headers") and self._nexus_extra_headers:
    kwargs.setdefault("extra_headers", {})
    kwargs["extra_headers"].update(self._nexus_extra_headers)
```

---

### 3.4 `aider/coders/base_coder.py` — Two Modifications

**Edit 1** — New method `check_for_skills()` (~line 912):
```python
def check_for_skills(self, inp):
    """Detect @name tags to switch the active product context (skill) mid-session."""
    import re
    pattern = r"@(\w+)"
    matches = re.findall(pattern, inp)
    if not matches:
        return inp

    import requests
    from aider.nexus_auth import NEXUS_BASE_URL, update_skill_mapping

    for skill_name in matches:
        try:
            headers = {}
            if hasattr(self.main_model, "_nexus_extra_headers"):
                headers = dict(self.main_model._nexus_extra_headers)

            resp = requests.get(
                f"{NEXUS_BASE_URL}/api/skills/{skill_name}",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                if hasattr(self.main_model, "_nexus_extra_headers"):
                    self.main_model._nexus_extra_headers["X-Nexus-Skill"] = skill_name
                repo_root = getattr(self, "root", None)
                if repo_root:
                    update_skill_mapping(repo_root, skill_name)
                self.io.tool_output(f"Switched product context to: {skill_name}")
            else:
                self.io.tool_warning(f"Skill @{skill_name} not found (HTTP {resp.status_code})")
        except Exception as e:
            self.io.tool_warning(f"Failed to load skill @{skill_name}: {e}")

    cleaned = re.sub(r"@\w+\s*", "", inp).strip()
    return cleaned
```

**Edit 2** — Hook into `preproc_user_input()` (~line 963):
```python
def preproc_user_input(self, inp):
    if not inp:
        return
    if self.commands.is_command(inp):
        return self.commands.run(inp)

    # Nexus CLI: check for @skill tags to switch product context
    inp = self.check_for_skills(inp)
    if not inp:
        return
    # ... rest of original method ...
```

---

### 3.5 `aider/commands.py` — One New Method

New method `cmd_solve()` added to the `Commands` class (~line 1682):
```python
def cmd_solve(self, args):
    """Submit an error/issue to the Nexus AgentOverflow API for analysis"""
    import subprocess
    import requests
    from aider.nexus_auth import NEXUS_BASE_URL

    error_description = args.strip() if args and args.strip() else "No description provided"

    # Capture git diff
    git_diff = ""
    try:
        result = subprocess.run(
            ["git", "diff", "--no-color"],
            cwd=self.coder.root,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            git_diff = result.stdout
    except Exception:
        pass

    try:
        files_in_context = list(self.coder.get_inchat_relative_files())
    except Exception:
        files_in_context = []

    payload = {
        "description": error_description,
        "git_diff": git_diff,
        "files_in_context": files_in_context,
    }

    headers = {"Content-Type": "application/json"}
    if hasattr(self.coder.main_model, "_nexus_extra_headers"):
        headers.update(self.coder.main_model._nexus_extra_headers)

    try:
        resp = requests.post(
            f"{NEXUS_BASE_URL}/api/overflow/ingest",
            json=payload, headers=headers, timeout=30,
        )
        if resp.status_code == 200:
            result = resp.json()
            self.io.tool_output("Overflow analysis submitted successfully.")
            suggestion = result.get("suggestion", "")
            if suggestion:
                self.io.tool_output(suggestion)
        else:
            self.io.tool_error(f"Overflow API returned HTTP {resp.status_code}")
    except Exception as e:
        self.io.tool_error(f"Failed to reach overflow API: {e}")
```

---

### 3.6 `aider/nexus_auth.py` — Entirely New File

This module handles skill detection and config caching. Authentication is handled server-side — no credentials are stored or transmitted from the CLI. Key public API:

```python
NEXUS_BASE_URL = "http://localhost:8000"  # Change for production

def get_auth_headers(io=None) -> dict:
    """Returns {} — auth is handled server-side by the backend service account."""

def detect_active_skill(io, repo_root) -> str:
    """
    Auto-detect which product skill applies to this repo.
    1. Check ~/.nexus/config skill_mappings cache
    2. If not cached: GET /api/skills, score each against repo metadata
    3. Auto-select if score > 10 AND 2x better than second place
    4. Else: prompt user
    Returns skill name string e.g. "staking"
    """

def update_skill_mapping(repo_root: str, skill_name: str) -> None:
    """Cache repo → skill mapping in ~/.nexus/config"""
```

Config file at `~/.nexus/config` (mode 0o600):
```json
{
  "skill_mappings": {
    "/absolute/path/to/repo": "staking"
  }
}
```

---

## 4. Backend Contract (What the Backend Must Implement)

The CLI expects a FastAPI server at `http://localhost:8000`. For production, change `NEXUS_BASE_URL` in `aider/nexus_auth.py` and the hardcoded URL in `aider/main.py`.

### 4.1 Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | LLM chat — OpenAI-compatible SSE streaming |
| `GET` | `/api/skills` | List available skills (for auto-detection) |
| `GET` | `/api/skills/{name}` | Fetch a specific skill's markdown content |
| `POST` | `/api/overflow/ingest` | Receive error traces for analysis |
| `GET` | `/v1/models` | Health check (returns model list) |

### 4.2 Headers on Every Request

The CLI sends only the skill header:
```
X-Nexus-Skill: staking          (whichever skill is active)
```

No auth headers are sent from the CLI. The backend authenticates via its own service account. The backend uses `X-Nexus-Skill` to load the right product context.

### 4.3 `POST /v1/chat/completions` — THE CRITICAL ENDPOINT

**Request** (standard OpenAI format):
```json
{
  "model": "nexus-agent",
  "stream": true,
  "temperature": 0.7,
  "messages": [
    {
      "role": "system",
      "content": "Act as an expert software developer...\n[aider's SEARCH/REPLACE format rules]\n..."
    },
    {
      "role": "user",
      "content": "<repo-map>\n...\n</repo-map>\n\nfile.py\n```python\n...\n```"
    },
    {
      "role": "user",
      "content": "Fix the bug in the validator"
    }
  ]
}
```

**⚠️ CRITICAL — MESSAGE INJECTION RULE:**
- `messages[0]` is ALWAYS aider's system prompt containing SEARCH/REPLACE format rules
- **DO NOT modify or replace `messages[0]`**
- **Inject RAG context as a NEW `messages[1]`** (insert, don't append to messages[0])
- If `messages[0]` is altered or RAG replaces it, the LLM will stop producing SEARCH/REPLACE blocks and all file edits will fail

**Correct backend injection pattern:**
```python
# messages[0] = aider's system prompt (UNTOUCHED)
# messages[1] = YOUR RAG context (INSERT HERE)
# messages[2..N] = original user messages (shifted up by 1)

rag_message = {
    "role": "system",
    "content": f"## Product Context: {skill_name}\n\n{skill_content}\n\n## Relevant Docs\n\n{confluence_chunks}"
}
messages_to_send = [messages[0], rag_message] + messages[1:]
```

**Response** (OpenAI SSE streaming format):
```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"},"index":0}]}
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","choices":[{"delta":{"content":"Here is the fix:\n\n"},"index":0}]}
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","choices":[{"delta":{"content":"src/validator.py\n```python\n<<<<<<< SEARCH\nold_code\n=======\nnew_code\n>>>>>>> REPLACE\n```"},"index":0}]}
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop","index":0}]}
data: [DONE]
```

**The LLM driving the backend MUST return SEARCH/REPLACE blocks.** Example:
```
src/validator.py
```python
<<<<<<< SEARCH
def validate(x):
    return x > 0
=======
def validate(x):
    if x is None:
        raise ValueError("x cannot be None")
    return x > 0
>>>>>>> REPLACE
```
```

### 4.4 `GET /api/skills`

Returns list of available skills (used for auto-detection):
```json
[
  {
    "name": "staking",
    "keywords": ["stake", "validator", "delegation", "consensus"],
    "description": "Staking product context"
  },
  {
    "name": "payments",
    "keywords": ["payment", "settlement", "transaction", "ledger"],
    "description": "Payments product context"
  }
]
```

### 4.5 `GET /api/skills/{name}`

Returns skill content (markdown rules, standards, context):
```json
{
  "name": "staking",
  "skill_content": "# Staking Product Rules\n\n## Code Standards\n...\n\n## Architecture Notes\n..."
}
```

The `skill_content` is what gets injected as RAG context (see 4.3).

### 4.6 `POST /api/overflow/ingest`

Receives error submissions from `/solve` command:
```json
{
  "description": "The token refresh is returning 403 with valid credentials",
  "git_diff": "diff --git a/src/auth.py ...",
  "files_in_context": ["src/auth.py", "src/middleware.py"]
}
```

Response:
```json
{
  "status": "received",
  "ticket_id": "NEXUS-1234",
  "suggestion": "This looks like a clock skew issue. Check server time sync."
}
```

### 4.7 `GET /v1/models` (Health Check)

CLI polls this on startup to verify backend is reachable:
```json
{
  "object": "list",
  "data": [{"id": "nexus-agent", "object": "model"}]
}
```

---

## 5. How @skill Works (End-to-End)

```
User types: "@staking fix the validator"
    │
    ▼
base_coder.preproc_user_input()
    │
    ▼
check_for_skills() detects "@staking"
    │
    ├─► GET /api/skills/staking
    │   Response: {"skill_content": "# Staking Rules\n..."}
    │
    ├─► Updates X-Nexus-Skill header to "staking" for all future requests
    ├─► Caches in ~/.nexus/config skill_mappings
    │
    ▼
Strips "@staking" from message → "fix the validator"
    │
    ▼
Normal aider flow continues with cleaned message
    │
    ▼
POST /v1/chat/completions with X-Nexus-Skill: staking
Backend sees header → loads staking context → injects into messages[1]
```

> **Note**: The skill content from `/api/skills/{name}` is NOT directly injected by the CLI into the messages. The CLI only updates the `X-Nexus-Skill` header. The **backend** is responsible for loading and injecting the skill content into `messages[1]`. This keeps the CLI thin.

---

## 6. How /solve Works (End-to-End)

```
User types: "/solve auth middleware returning 403"
    │
    ▼
commands.cmd_solve("auth middleware returning 403")
    │
    ├─► git diff --no-color  (captures current uncommitted changes)
    ├─► coder.get_inchat_relative_files()  (files in current context)
    │
    ▼
POST /api/overflow/ingest
{
  "description": "auth middleware returning 403",
  "git_diff": "diff --git a/...",
  "files_in_context": ["src/auth.py"]
}
Headers: X-Nexus-Skill
    │
    ▼
Prints: "Overflow analysis submitted successfully."
Prints: suggestion from response (if any)
```

---

## 7. Skill Auto-Detection Algorithm

On first run in a repo (no cached mapping):

```python
# 1. Fetch skills list: GET /api/skills
skills = [{"name": "staking", "keywords": [...]}, ...]

# 2. Get repo metadata
metadata = {
    "dir_name": "staking-contracts",        # os.path.basename(repo_root)
    "git_remote": "github.com/org/staking", # git remote -v
    "top_files": ["validator.rs", "stake.rs", ...]
}

# 3. Score each skill
def _score_skill(skill, metadata):
    score = 0
    keywords = skill["keywords"] + [skill["name"]]
    dir_name = metadata["dir_name"].lower()

    for kw in keywords:
        if kw in dir_name: score += 10         # strong signal
        if kw in metadata["git_remote"]: score += 8
        for f in metadata["top_files"]:
            if kw in f.lower(): score += 3

    return score

# 4. Auto-select if confident
scores = {s["name"]: _score_skill(s, metadata) for s in skills}
best = max(scores, key=scores.get)
second = sorted(scores.values())[-2] if len(scores) > 1 else 0

if scores[best] > 10 and scores[best] >= 2 * max(second, 1):
    return best   # auto-selected
else:
    # Prompt user to choose
```

---

## 8. Running Locally

### Prerequisites
```bash
# Install UV
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Run CLI (dev mode)
```bash
cd nexus-cli
uv sync
uv run nexus --version
# nexus 0.86.3.dev34+gbdb4d9ff8.d20260328

uv run nexus
# Detects product context, checks backend health, opens chat
```

### Run Mock Backend (for testing without real Nexus backend)
```bash
uv run python mock_backend.py
# Starts on http://localhost:8000
# Logs all requests — useful for seeing what CLI sends

# In another terminal:
uv run nexus
```

### Run Tests
```bash
uv run python test_integration.py
# Tests: auth module, hardwiring, @skill regex, /solve structure, OpenAPI spec
```

### Build Standalone Binary
```bash
uv run python build_nexus.py
# Output: dist/nexus  (single file, no Python needed)
./dist/nexus --version
```

---

## 9. Configuration

### Backend URL

Change in **two places** for production:
1. `aider/nexus_auth.py` line 15: `NEXUS_BASE_URL = "http://localhost:8000"`
2. `aider/main.py` line 510: `os.environ["OPENAI_API_BASE"] = "http://localhost:8000/v1"`

Both should point to the same backend. Consider making this an env var:
```python
NEXUS_BASE_URL = os.environ.get("NEXUS_BACKEND_URL", "http://localhost:8000")
```

### Auth Header Names

Currently using:
```


X-Nexus-Skill
```

Auth is handled server-side. To add custom headers, update `get_auth_headers()` in `aider/nexus_auth.py`.

---

## 10. Common Tasks for Agents

### "Change the backend URL to production"
1. Edit `aider/nexus_auth.py` line 15: `NEXUS_BASE_URL = "https://nexus.prod.internal"`
2. Edit `aider/main.py` line 510: `os.environ["OPENAI_API_BASE"] = "https://nexus.prod.internal/v1"`
3. Edit `aider/main.py` health check line: update the `localhost:8000` URL to match

### "Add a new custom command /foo"
1. Add `cmd_foo(self, args)` method to `Commands` class in `aider/commands.py`
2. Follow the pattern of `cmd_solve()` — use `self.coder`, `self.io.tool_output()`, `requests`
3. Add to `README.md` command reference table

### "Add a new skill keyword"
This is a backend concern — update the skill definitions returned by `GET /api/skills`. No CLI changes needed.

### "Make backend URL configurable via env var"
1. `aider/nexus_auth.py`: `NEXUS_BASE_URL = os.environ.get("NEXUS_BACKEND_URL", "http://localhost:8000")`
2. `aider/main.py` block 1: `os.environ["OPENAI_API_BASE"] = os.environ.get("NEXUS_BACKEND_URL", "http://localhost:8000") + "/v1"`
3. `aider/main.py` block 2 health check: use the same env var

---

## 11. What NOT to Change

| File / Area | Why |
|-------------|-----|
| `aider/coders/` (except the two edits) | Core aider edit format logic — changes break file editing |
| `messages[0]` content | Contains SEARCH/REPLACE format rules — LLM relies on this |
| `aider/` package name | Renaming would require hundreds of import changes |
| `openai/nexus-agent` model prefix | The `openai/` prefix routes through litellm's OpenAI provider |
| `.venv/` | Auto-generated by UV, committed to .gitignore |

---

## 12. Known Limitations & TODOs

| Item | Status | Notes |
|------|--------|-------|
| Backend URL env var | Hardcoded | Two places need to be updated for production — see §10 |
| Skill content client-side injection | Not implemented | Currently only updates `X-Nexus-Skill` header; backend does injection |
| Windows binary | Not tested | PyInstaller config may need adjustments for Windows paths |
| Upstream aider merges | Manual | Need to periodically merge upstream aider improvements |

---

## 13. File Quick Reference

| File | Lines | What It Does |
|------|-------|--------------|
| `aider/nexus_auth.py` | ~200 | Skill detection, config cache (no credentials) |
| `aider/main.py` | ~1200 | CLI entry point — two nexus blocks at ~509 and ~843 |
| `aider/models.py` | ~900 | LLM model abstraction — nexus bypass at ~728, headers at ~1020 |
| `aider/coders/base_coder.py` | ~1000 | Core edit loop — `check_for_skills()` at ~912, hook at ~963 |
| `aider/commands.py` | ~1700 | All `/` commands — `cmd_solve()` at ~1682 |
| `mock_backend.py` | ~238 | Local test backend — run with `uv run python mock_backend.py` |
| `test_integration.py` | ~405 | Integration tests — run with `uv run python test_integration.py` |
| `build_nexus.py` | ~56 | PyInstaller config — run with `uv run python build_nexus.py` |
| `docs/nexus-backend-openapi.yaml` | ~352 | Full OpenAPI 3.1 spec |
| `BACKEND_CONTEXT.md` | ~600 | Deep backend implementation guide (read before touching backend) |

---

## 14. Test the Setup in 3 Commands

```bash
# 1. Install
uv sync

# 2. Start mock backend
uv run python mock_backend.py &

# 3. Run CLI
uv run nexus
```

---

*Last updated: March 28, 2026 — Installation verified on Python 3.14.3 with UV.*
