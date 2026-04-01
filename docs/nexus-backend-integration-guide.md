# Nexus Backend Integration Guide

## Overview

This document defines the contract between the **Nexus CLI** (an aider-chat fork) and the **Nexus FastAPI backend**. The backend team must implement endpoints that the CLI expects, with strict adherence to the message handling rules below.

**Architecture**:
```
Developer's Terminal
    └── Nexus CLI (aider fork)
            │
            ├── POST /v1/chat/completions  (LLM requests with SSE streaming)
            ├── GET  /api/skills            (list available product contexts)
            ├── GET  /api/skills/{name}     (validate/fetch single skill)
            ├── POST /api/overflow/ingest   (error analysis submission)
            └── GET  /v1/models             (health check)
            │
    Nexus FastAPI Backend
            │
            ├── Internal Auth Proxy (auth via username/password)
            ├── Lumin8 (LLM routing wrapper)
            └── RAG Pipeline (Confluence + code embeddings + SKILLS.md)
```

---

## CRITICAL: The Edit Format Constraint

Aider uses a strict **SEARCH/REPLACE block format** to edit local files. The system prompt sent by the CLI contains these formatting rules. **If the backend modifies or removes this system prompt, the CLI will fail to apply any file edits.**

### The SEARCH/REPLACE Format

The LLM must return code changes in this exact format:

```
path/to/file.py
```python
<<<<<<< SEARCH
def old_function():
    return "old"
=======
def new_function():
    return "new"
>>>>>>> REPLACE
```
```

### Rules the LLM Must Follow (from aider's system prompt):

1. File path alone on a line (full path as shown by the user)
2. Opening fence with language: `` ```python ``
3. `<<<<<<< SEARCH` marker
4. Exact existing code to find (character-for-character match)
5. `=======` divider
6. Replacement code
7. `>>>>>>> REPLACE` marker
8. Closing fence: `` ``` ``

**The SEARCH section must EXACTLY MATCH existing file content.** The CLI uses multiple matching strategies (exact, fuzzy, diff-based) but works best with exact matches.

---

## Message Handling: How to Inject RAG Context

### The Messages Array Structure

The CLI sends messages in this order (via the `ChatChunks` system):

```
Index  Role      Content
─────  ────      ───────
0      system    Aider's main system prompt (SEARCH/REPLACE rules + behavior instructions)
1..N   user      Example file contents, repo map, readonly files, chat history
N+1    user      Current user message (the actual developer request)
```

### Backend MUST Follow This Pattern:

```python
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body["messages"]
    skill = request.headers.get("X-Nexus-Skill", "default")

    # 1. PRESERVE messages[0] — this is aider's system prompt with edit format rules
    aider_system_prompt = messages[0]

    # 2. BUILD your RAG context message
    skills_md = load_skills_md(skill)           # e.g., STAKING_SKILLS.md
    confluence_chunks = search_confluence(messages[-1]["content"])
    code_standards = load_code_standards(skill)

    rag_context = f"""
## Product Context: {skill}

### Skills & Architecture Rules
{skills_md}

### Relevant Confluence Documentation
{confluence_chunks}

### Code Standards
{code_standards}

IMPORTANT: You MUST format all code changes using SEARCH/REPLACE blocks as described
in the system prompt above. Do not use any other format.
"""

    rag_message = {"role": "system", "content": rag_context}

    # 3. INSERT after aider's system prompt, before everything else
    augmented_messages = [aider_system_prompt, rag_message] + messages[1:]

    # 4. Forward to LLM via Lumin8 with streaming
    return StreamingResponse(
        stream_from_lumin8(augmented_messages, body),
        media_type="text/event-stream"
    )
```

### What NOT to Do:

```python
# WRONG: Replacing the system prompt
messages[0] = {"role": "system", "content": your_custom_prompt}

# WRONG: Prepending before aider's system prompt
messages.insert(0, {"role": "system", "content": rag_context})

# WRONG: Appending RAG context to aider's system prompt content
messages[0]["content"] += "\n\n" + rag_context
# (This technically works but can push critical rules out of the attention window)
```

### Recommended: Use a Separate System Message

Insert your RAG context as `messages[1]` with `role: "system"`. This keeps aider's formatting rules in `messages[0]` untouched and places your product context right after, where it has high attention priority.

---

## SSE Streaming Format

The endpoint **must** support Server-Sent Events (SSE) streaming in the standard OpenAI format. The CLI's `show_send_output_stream()` method consumes chunks like this:

### Chunk Format:

```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{"role":"assistant","content":"Here "},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{"content":"is the fix:"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### Key Requirements:

- Each line is prefixed with `data: `
- Each chunk is separated by `\n\n`
- `choices[0].delta.content` contains the text fragment
- `finish_reason` is `null` during streaming, `"stop"` on completion
- Stream terminates with `data: [DONE]`
- If the response is cut short by context limits, `finish_reason` should be `"length"`

### What the CLI Extracts from Each Chunk:

```python
# From aider/coders/base_coder.py show_send_output_stream():
for chunk in completion:
    delta = chunk.choices[0].delta
    content = delta.content        # Main text content
    # Also checks: delta.reasoning_content, delta.function_call
```

---

## Authentication

The CLI sends **no auth headers**. Authentication is handled entirely server-side — the backend uses its own service account / internal token. The only header the CLI sends is:

| Header | Description |
|--------|-------------|
| `X-Nexus-Skill` | Active product context name (e.g. `"staking"`). Present on every request. |

The backend should validate the service-account token at startup and use it for all outbound calls to Lumin8 and Confluence.

---

## Endpoint Reference

### 1. `POST /v1/chat/completions`

**Purpose**: Main LLM endpoint (OpenAI-compatible)

| Field | Type | Notes |
|-------|------|-------|
| `model` | string | Always `"nexus-agent"` |
| `messages` | array | See message handling section above |
| `stream` | boolean | Almost always `true` |
| `temperature` | float | Usually `0` |

### 2. `GET /api/skills`

**Purpose**: List all available product contexts for auto-detection

**Response**: Array of skill objects:
```json
[
  {
    "name": "staking",
    "description": "Staking product - validator management and delegation",
    "keywords": ["stake", "validator", "delegation", "epoch"]
  }
]
```

The `keywords` array is used by the CLI to auto-match against repository metadata (directory name, git remote URL, filenames).

### 3. `GET /api/skills/{name}`

**Purpose**: Validate a skill exists (used when user types `@skillname`)

**Response** (200):
```json
{
  "name": "staking",
  "description": "Staking product",
  "skill_content": "# Staking Skills\n\n## Architecture Rules\n..."
}
```

**Response** (404): Skill not found

### 4. `POST /api/overflow/ingest`

**Purpose**: Receive a developer query from `/solve` and **always return an answer immediately**. Internally the backend applies a semantic cache decision — no human approval or staging is required at any point. The developer never waits for or is aware of this decision.

#### Request Body

```json
{
  "description":    "Auth middleware returns 403 after token refresh",

  "git_diff":       "diff --git a/auth.py b/auth.py\n...",
  "dirty_files":    ["src/auth.py"],
  "recent_commits": ["fix: token refresh", "chore: bump deps"],

  "all_files":      ["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
  "chat_files":     ["src/auth.py"],
  "ident_mentions": ["refresh_token", "AuthMiddleware"],
  "file_mentions":  ["auth.py"],

  "lint_outcome":   null,
  "test_outcome":   false,

  "recent_messages": [
    {"role": "user", "content": "The refresh endpoint keeps 403-ing"},
    {"role": "assistant", "content": "..."}
  ]
}
```

Field notes:

| Field | Source | Notes |
|---|---|---|
| `description` | user text | Primary embedding input for similarity search |
| `git_diff` | `repo.get_diffs()` | May be empty — never block on this |
| `dirty_files` | `repo.get_dirty_files()` | Files modified since last commit |
| `recent_commits` | `git log --oneline -5` | Temporal context |
| `all_files` | `coder.get_all_relative_files()` | Full git-tracked codebase (mirrors aider repo-map universe) |
| `chat_files` | `coder.get_inchat_relative_files()` | Explicitly `/add`'d files — 50× PageRank boost in aider |
| `ident_mentions` | `coder.get_ident_mentions(description)` | Identifiers extracted from user text |
| `file_mentions` | `coder.get_file_mentions(description)` | Filenames extracted from user text |
| `lint_outcome` | `coder.lint_outcome` | `null`=not run, `true`=pass, `false`=fail |
| `test_outcome` | `coder.test_outcome` | Same |
| `recent_messages` | `coder.done_messages[-6:]` | Last 3 conversation turns for retry context |

#### Semantic Cache Decision Tree (Backend Logic)

```
RECEIVE query
    │
    ▼
EMBED description  (+ ident_mentions as boosting context)
    │
    ▼
COSINE SIMILARITY SEARCH against existing KB solutions
    │
    ├─ similarity ≥ 0.87  ──► CACHE HIT
    │                          Return stored solution immediately
    │                          cached=true, persisted=false
    │
    ├─ 0.65 ≤ similarity < 0.87  ──► SIMILAR EXISTS
    │                               Call LLM (RAG + context injection)
    │                               DEDUP FLAG set (do not store even if confident)
    │                               cached=false, persisted=false
    │
    └─ similarity < 0.65  ──► NOVEL QUERY
                               Call LLM (RAG + context injection)
                               Compute confidence signal from response
                               │
                               ├─ confidence ≥ 0.72 AND response_length > 80 tokens
                               │   AND test_outcome != false   ──► PERSIST TO KB
                               │   cached=false, persisted=true
                               │
                               └─ otherwise  ──► SERVE ONLY
                                   cached=false, persisted=false
```

#### RAG Context Injection for LLM Call

When an LLM call is needed, build context from the request fields:

```python
# Rank files by relevance signal (mirrors aider's repo-map PageRank logic)
# chat_files → 50× boost, ident_mentions / file_mentions → 10× boost
ranked_files = rank_by_relevance(all_files, chat_files, ident_mentions, file_mentions)

# Retrieve top matching KB entries for the query
kb_entries = vector_search(description, top_k=5, threshold=0.55)

# Inject as RAG system message (after aider's system prompt — same pattern as /chat)
rag_context = build_rag_message(kb_entries, skill_content, ranked_files[:20])
```

#### Confidence Signal

After the LLM responds, compute a confidence score used in the storage decision:

```python
def compute_confidence(response_text, lint_outcome, test_outcome):
    score = 0.5   # base

    # Response length: longer answers tend to be more actionable
    tokens = len(response_text.split())
    if tokens > 200:  score += 0.15
    elif tokens > 80: score += 0.10

    # Contains code (SEARCH/REPLACE blocks or inline code)
    if "<<<<<<< SEARCH" in response_text or "```" in response_text:
        score += 0.15

    # Test/lint outcome: passing = higher signal that context was good
    if test_outcome is True:   score += 0.10
    if lint_outcome is True:   score += 0.05
    if test_outcome is False:  score -= 0.10  # environment broken, lower trust

    return min(score, 1.0)
```

#### TTL and Deduplication Strategy

- **TTL**: All persisted entries have a flat **6-month TTL**, reset on each cache hit.
- **Deduplication gate**: Before storing any new solution, run a final similarity check at threshold 0.80. If a match exists, update that entry's `hit_count` instead of creating a duplicate.
- **No staging area**: Decisions are made automatically at ingest time. If the LLM response is confident enough, it is cached immediately — no human approval or secondary promote step is required.
- **Enrichment on resolution**: When a developer later calls `/solved` or `/commit`, the stored entry is enriched with the real committed diff + LLM-generated summary, replacing the original AI answer. This improves future retrieval quality without changing when the entry became searchable.

#### Response

```json
{
  "status": "ok",
  "issue_id": "3f7a2b1c-...",
  "suggestion": "The token expiry check runs before the refresh call on line 47...",
  "cached": false,
  "cache_hit_similarity": null,
  "confidence_score": 0.82,
  "persisted": true
}
```

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"ok"` or `"error"` |
| `issue_id` | string | UUID for /solved resolution |
| `suggestion` | string | The solution text shown to the developer |
| `cached` | bool | `true` = served from KB cache |
| `cache_hit_similarity` | float\|null | Cosine score of matched entry (null if not cached) |
| `confidence_score` | float\|null | Model confidence 0–1 (null if cached) |
| `persisted` | bool | `true` = new entry stored in KB |

### 5. `GET /v1/models`

**Purpose**: Health check — CLI hits this at startup to verify backend is reachable

**Response**:
```json
{
  "data": [
    {"id": "nexus-agent", "object": "model"}
  ]
}
```

---

## FastAPI Skeleton for Backend Team

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import json, uuid
from datetime import datetime, timedelta

app = FastAPI(title="Nexus Backend", version="1.0.0")

# NOTE: No auth from CLI — backend authenticates via its own service account.
# The CLI only sends X-Nexus-Skill on every request.

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body["messages"]
    skill = request.headers.get("X-Nexus-Skill", "default")
    model = body.get("model", "nexus-agent")

    # TODO: Load SKILLS.md for the active skill
    # TODO: Search Confluence for relevant chunks based on user message
    # TODO: Search AgentOverflow KB for past fixes (search_overflow)
    # TODO: Insert RAG context as messages[1] (see integration guide)
    # TODO: Route to correct agent via model name:
    #   "nexus-architect" → plan, NO SEARCH/REPLACE
    #   "nexus-agent"    → SEARCH/REPLACE blocks required
    # TODO: Forward augmented messages to LLM via Lumin8
    # TODO: Stream response back as SSE

    async def generate():
        # Example stub — replace with actual Lumin8 streaming
        yield 'data: {"choices":[{"delta":{"content":"Hello from Nexus"},"finish_reason":null}]}\n\n'
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/skills")
async def list_skills(request: Request):
    # TODO: Return all available product skills from database/config
    return [
        {"name": "staking", "description": "Staking product", "keywords": ["stake", "validator"]},
    ]


@app.get("/api/skills/{name}")
async def get_skill(name: str, request: Request):
    # TODO: Look up skill by name, return 404 if not found
    return {"name": name, "description": "...", "skill_content": "..."}


@app.post("/api/overflow/ingest")
async def overflow_ingest(request: Request):
    body = await request.json()
    description    = body.get("description", "")
    git_diff       = body.get("git_diff", "")
    dirty_files    = body.get("dirty_files", [])
    recent_commits = body.get("recent_commits", [])
    all_files      = body.get("all_files", [])
    chat_files     = body.get("chat_files", [])
    ident_mentions = body.get("ident_mentions", [])
    file_mentions  = body.get("file_mentions", [])
    lint_outcome   = body.get("lint_outcome")
    test_outcome   = body.get("test_outcome")
    recent_msgs    = body.get("recent_messages", [])
    skill          = request.headers.get("X-Nexus-Skill", "default")

    # Semantic cache decision — fully automatic, no human in the loop:
    # TODO: 1. Embed description (+ ident_mentions as boosting context)
    # TODO: 2. Cosine similarity search against ChromaDB "overflow_cache" collection
    #   ≥ 0.87  → CACHE HIT: return stored answer (cached=True, no LLM call)
    #   0.65–0.87 → SIMILAR: call LLM, serve answer, skip persist (persisted=False)
    #   < 0.65  → NOVEL: call LLM, auto-persist if confidence ≥ 0.72
    # TODO: 3. Rank all_files by relevance (chat_files 50×, ident/file mentions 10×)
    # TODO: 4. Build RAG context: ranked files + KB hits + skill content
    # TODO: 5. Call LLM via Lumin8 with RAG context
    # TODO: 6. Compute confidence: base 0.50 + length bonus + code bonus + lint/test signals
    # TODO: 7. Apply deduplication gate at 0.80 before storing
    # TODO: 8. Auto-persist novel+confident entries with flat 6-month TTL (no staging area)
    return {
        "status": "ok",
        "issue_id": str(uuid.uuid4()),
        "suggestion": "",
        "cached": False,
        "cache_hit_similarity": None,
        "confidence_score": None,
        "persisted": False,
    }


@app.post("/api/overflow/resolve")
async def overflow_resolve(request: Request):
    body = await request.json()
    issue_id       = body.get("issue_id", "")
    committed_diff = body.get("committed_diff", "")  # auto path: /commit
    resolution     = body.get("resolution", "")       # explicit path: /solved <note>

    if not issue_id:
        raise HTTPException(status_code=422, detail="issue_id is required.")
    if not committed_diff and not resolution:
        raise HTTPException(status_code=422, detail="Provide committed_diff or resolution.")

    # TODO: 1. Look up issue in SQLite by issue_id (404 if not found or expired)
    # TODO: 2. If resolution present → use it directly (explicit wins over inferred)
    # TODO: 3. If only committed_diff → call LLM to summarize:
    #         prompt = "An engineer was debugging: {description}\n"
    #                  "They committed: {diff}\n"
    #                  "In 1-2 sentences, explain what the fix was."
    # TODO: 4. UPDATE overflow_issues SET resolution=?, resolved_at=now(), expires_at=+6mo
    # TODO: 5. overflow_collection.upsert(id, "Issue: ...\nResolution: ...", metadata)
    return {
        "status": "resolved",
        "message": "Fix captured. The knowledge base will surface this for similar future issues.",
    }


@app.get("/v1/models")
async def list_models():
    return {"data": [{"id": "nexus-agent", "object": "model"}]}
```

---

## Testing the Integration

1. **Start the backend**: `uvicorn main:app --host 0.0.0.0 --port 8000`
2. **Run the CLI**: `nexus` (from the nexus-cli repo with `pip install -e .`)
3. **Verify**: The CLI should:
   - Prompt for the internal auth proxy credentials on first run
   - Auto-detect or ask for product context
   - Show "Active product context: staking"
   - Accept user input and stream responses
   - Apply SEARCH/REPLACE edits to local files

### Debugging Tips

- Set `--verbose` flag on the CLI to see full message payloads
- Check `X-Nexus-Skill` header in backend request logs
- If edits fail to apply: the LLM response is not following SEARCH/REPLACE format — check that your RAG injection isn't displacing the system prompt
- If streaming breaks: ensure `data: ` prefix and `\n\n` separators between chunks
