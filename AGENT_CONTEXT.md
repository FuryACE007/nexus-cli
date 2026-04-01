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

### 3.5 `aider/commands.py` — Two New Methods (`cmd_solve`, `cmd_solved`)

`cmd_solve()` always returns an answer immediately. The backend runs the **semantic cache
decision automatically** — no human in the loop, no staging area. The developer just gets
an answer.

**Design principle**: aider's repo-map already provides 8× larger token budget when no files are manually `/add`'d — forcing `/add` before `/solve` would punish the correct usage pattern. `cmd_solve` therefore sends the **full codebase structure** (`get_all_relative_files()`), not just explicitly added files.

**Payload sent to backend**:
```python
payload = {
    # Core query
    "description": error_description,
    # Git context
    "git_diff":       repo.get_diffs(),            # may be empty — soft warn only
    "dirty_files":    repo.get_dirty_files(),
    "recent_commits": ["last 5 git log lines"],    # git log --oneline -5
    # Codebase structure (mirrors aider repo-map universe)
    "all_files":      coder.get_all_relative_files(),    # full git-tracked list
    "chat_files":     coder.get_inchat_relative_files(), # explicitly /add'd (50× PageRank boost)
    "ident_mentions": coder.get_ident_mentions(description),  # identifiers in description
    "file_mentions":  coder.get_file_mentions(description),   # filenames in description
    # Session quality signals (used by backend for confidence scoring)
    "lint_outcome":   coder.lint_outcome,     # None / True / False
    "test_outcome":   coder.test_outcome,
    # Conversation context (last 3 exchanges for retry/escalation context)
    "recent_messages": (coder.done_messages + coder.cur_messages)[-6:],
}
```

**Backend semantic cache decision tree** (fully automatic, no human in the loop):
```
EMBED description (+ ident_mentions) → COSINE SIMILARITY SEARCH
    ≥ 0.87  → CACHE HIT   (return stored answer, skip LLM,  cached=True,  persisted=False)
  0.65–0.87 → SIMILAR     (call LLM, serve answer, skip persist,           persisted=False)
    < 0.65  → NOVEL       (call LLM + RAG, compute confidence)
                              confidence ≥ 0.72 → AUTO-PERSIST, 6-month TTL (persisted=True)
                              confidence < 0.72 → SERVE ONLY               (persisted=False)
```

**TTL**: All persisted entries have a flat **6-month TTL**, reset on each cache hit.
**Deduplication gate**: Before storing, a final similarity check at 0.80 prevents duplicates.

**Response fields** the CLI reads:
- `suggestion` — the answer shown to the developer (always populated)
- `issue_id` — stored for auto-resolve on `/commit` and `/solved`
- `cached` — `True` = served from KB cache (prints ⚡)
- `confidence_score` — if < 0.60, a low-confidence warning is surfaced
- `persisted` — `True` = novel entry auto-stored in team KB (prints 📚)

`cmd_solved()` enriches the cached entry with the real committed fix. On `/commit`, `cmd_solved` is triggered automatically (`_nexus_maybe_auto_resolve()`): it captures `git show HEAD` and sends it to `/api/overflow/resolve`. The backend uses the LLM to summarize the diff and upgrades the KB entry — no developer input needed.

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

### 3.7 Architect Mode Wiring — Two Model Instances

The `/architect` command uses a two-stage flow. Each stage routes to a **different backend agent** via the `model` field in the request body.

**Stage 1 (plan)** → `openai/nexus-architect` → backend architect agent → returns a **natural language plan** (NO SEARCH/REPLACE)

**Stage 2 (edit)** → `openai/nexus-agent` → backend code agent → returns **SEARCH/REPLACE blocks**

At startup (`aider/main.py`, in the nexus block), two model instances are created:

```python
# main_model = code model (openai/nexus-agent) — already created
main_model._nexus_extra_headers = nexus_headers

# architect model — same backend URL, routes to architect agent via model name
arch_model = models.Model("openai/nexus-architect", editor_model=False, weak_model=False)
arch_model._nexus_extra_headers = dict(nexus_headers)
arch_model.editor_model = main_model        # ArchitectCoder uses this for stage 2
arch_model.editor_edit_format = "diff"

# Cross-references for runtime mode switching in commands.py
main_model._nexus_architect_model = arch_model
arch_model._nexus_code_model = main_model
```

**Mode switching** (`aider/commands.py`):
- `cmd_architect()` → sets `self.coder.main_model = arch_model` before entering architect mode
- `cmd_code()`, `cmd_ask()`, `cmd_context()` → call `_restore_nexus_code_model()` which sets `self.coder.main_model = main_model`

**`@skill` sync** (`aider/coders/base_coder.py`): When the user types `@payments`, both model instances have their `X-Nexus-Skill` header updated so whichever is active at the time of the next request sends the correct skill.

**Validation bypass** (`aider/models.py`): Both `nexus-agent` and `nexus-architect` bypass litellm validation.

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

### 4.2 Headers and Model Name Routing

The CLI sends only one header:
```
X-Nexus-Skill: staking          (whichever skill is active)
```

No auth headers are sent from the CLI — the backend authenticates via its own service account.

**The backend must also route based on the `model` field in the request body:**

| `model` value | Backend routes to | Response format |
|---------------|-------------------|----------------|
| `nexus-agent` | Code agent | SEARCH/REPLACE blocks (required) |
| `nexus-architect` | Architect agent | Natural language plan only (NO SEARCH/REPLACE) |

⚠️ **Critical**: `nexus-architect` responses must NOT contain SEARCH/REPLACE blocks. The CLI feeds the plan text directly to `nexus-agent` as the next request for actual file editing.

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

Receives rich context from `/solve` and **always returns an answer immediately**. Backend runs
semantic cache decision automatically — no human in the loop.

Request (key fields):
```json
{
  "description":    "The token refresh is returning 403 with valid credentials",
  "git_diff":       "diff --git a/src/auth.py ...",
  "all_files":      ["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
  "chat_files":     ["src/auth.py"],
  "ident_mentions": ["refresh_token", "AuthMiddleware"],
  "file_mentions":  ["auth.py"],
  "lint_outcome":   null,
  "test_outcome":   false
}
```

Response:
```json
{
  "status": "ok",
  "issue_id": "a1b2c3d4-...",
  "suggestion": "Check that TokenRefreshMiddleware runs before AuthMiddleware...",
  "cached": false,
  "cache_hit_similarity": null,
  "confidence_score": 0.82,
  "persisted": true
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
    ├─► git diff + dirty_files + recent_commits (git context)
    ├─► coder.get_all_relative_files()          (full codebase — NOT just /add'd files)
    ├─► coder.get_inchat_relative_files()       (explicitly /add'd, higher signal)
    ├─► coder.get_ident_mentions(description)   (identifiers in description)
    ├─► coder.get_file_mentions(description)    (filenames in description)
    ├─► coder.lint_outcome, test_outcome        (session quality signals)
    ├─► (coder.done_messages + cur_messages)[-6:]  (recent conversation)
    │
    ▼
POST /api/overflow/ingest  (Headers: X-Nexus-Skill)
{
  "description": "auth middleware returning 403",
  "git_diff": "diff --git a/...",
  "all_files": ["src/auth.py", "src/middleware.py", ...],
  "chat_files": ["src/auth.py"],
  "ident_mentions": ["AuthMiddleware"],
  "file_mentions": ["auth.py"],
  "lint_outcome": null, "test_outcome": false,
  "recent_messages": [...]
}
    │
    ▼ Backend: embed → similarity search → cache hit or LLM → auto-persist if confident
    │
    ▼
Response: { suggestion, issue_id, cached, confidence_score, persisted }
    │
    ▼
CLI prints:
  ⚡ "Cache hit" (if cached=true)  OR  "🤖 Analyzed with LLM"
  📚 "Solution saved to KB" (if persisted=true)
  💡 The suggestion text
  Low-confidence warning if confidence_score < 0.60

Stores issue_id internally → auto-resolved on next /commit
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
