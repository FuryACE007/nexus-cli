# Nexus Backend Implementation Context

**For**: Backend Team / LLM Implementation Agents
**Purpose**: Complete context for implementing the Nexus FastAPI backend to integrate with the Nexus CLI

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Developer's Terminal: nexus command                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         │ HTTP  ·  X-Nexus-Skill header on every request
                         │
┌────────────────────────▼────────────────────────────────────┐
│ Nexus FastAPI Backend (localhost:8000)                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  1. LLM Request Handler (/v1/chat/completions)              │
│     ├─ Read X-Nexus-Skill header (active product)          │
│     ├─ Load SKILLS.md for that product                      │
│     ├─ Search Confluence for relevant chunks                │
│     ├─ Query AgentOverflow for similar past fixes          │
│     ├─ Append all RAG context to messages[1]               │
│     ├─ Forward augmented messages to LLM via Lumin8        │
│     └─ Stream response back as SSE                          │
│                                                              │
│  2. Skill Management (/api/skills, /api/skills/{name})     │
│     ├─ Product metadata + keywords (for auto-detection)     │
│     └─ Full SKILLS.md content (for context injection)       │
│                                                              │
│  3. AgentOverflow Semantic Cache                            │
│     ├─ /api/overflow/ingest  — always answers immediately  │
│     │   ├─ similarity ≥ 0.87 → cache hit (skip LLM)       │
│     │   ├─ 0.65-0.87 → LLM call, skip persist             │
│     │   └─ < 0.65 → LLM call, auto-persist if conf≥0.72  │
│     └─ /api/overflow/resolve — enrich entry w/ real fix   │
│                                                              │
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
   ┌─────────┐     ┌──────────┐     ┌──────────┐
   │ Vector  │     │  Lumin8  │     │ Conf-    │
   │   DB    │     │  (LLM    │     │ luence   │
   │(issues +│     │  Router) │     │  (Docs)  │
   │  fixes) │     └────┬─────┘     └──────────┘
   └─────────┘          │
                        ▼
                  ┌─────────────┐
                  │  LLM Models │
                  │ Claude, GPT │
                  │  Llama, etc │
                  └─────────────┘
```

---

## Complete CLI-Backend Interaction Flow

See [`docs/nexus-agentic-flow-sequence.mermaid`](./docs/nexus-agentic-flow-sequence.mermaid) for a detailed Mermaid sequence diagram covering all CLI interactions:

1. **Startup** — Skill detection, health check, skill metadata caching
2. **@skill mid-session switch** — `GET /api/skills/{name}`, updates model headers, retrieves new SKILLS.md
3. **Architect chat flow** — Plan generation with RAG context (SKILLS.md, Confluence, ChromaDB overflow)
4. **Editor edit flow** — SEARCH/REPLACE block generation and file application
5. **`/solve` command** — Issue ingest, similarity search, past fix suggestions
6. **`/commit` auto-resolve** — Zero-effort path: captures git diff, backend LLM summarizes, ChromaDB upsert
7. **`/solved` explicit note** — Optional override with developer-written explanation
8. **Future developer** — Demonstrates how the next engineer gets instant surfaced fixes on similar issues

The diagram shows all HTTP endpoints, request/response payloads, and how the vector DB and SQLite knowledge base stay in sync.

---

## Request/Response Flow: Chat Completions

### Client (CLI) Sends:

```http
POST /v1/chat/completions HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Nexus-Skill: staking

{
  "model": "nexus-agent",
  "stream": true,
  "temperature": 0,
  "messages": [
    {
      "role": "system",
      "content": "Act as an expert software developer...\n\n# SEARCH/REPLACE block Rules:\nEvery *SEARCH/REPLACE block* must use this format:\n1. The *FULL* file path alone on a line...\n<<<<<<< SEARCH\n...\n=======\n...\n>>>>>>> REPLACE\n..."
    },
    {
      "role": "user",
      "content": "I have added these files to the chat: src/validator.rs\n\nFix the delegation bug in the staking contract."
    }
  ]
}
```

### Backend Processing Steps:

```python
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    skill = request.headers.get("X-Nexus-Skill", "default")

    # STEP 1: Extract and preserve aider's system prompt
    messages = body["messages"]
    aider_system_prompt = messages[0]  # DO NOT MODIFY

    # STEP 2: Query AgentOverflow for similar past resolutions
    past_fixes = search_overflow(
        query=messages[-1]["content"],  # User's request
        skill=skill,
        limit=3
    )

    # STEP 3: Build RAG context for the product
    try:
        skills_md = load_skills_md(skill)  # STAKING_SKILLS.md
        confluence_chunks = search_confluence(
            query=messages[-1]["content"],  # User's request
            limit=5
        )
        code_standards = load_code_standards(skill)
    except Exception as e:
        # Graceful degradation: continue without RAG
        skills_md = confluence_chunks = code_standards = ""

    # STEP 4: Create RAG context message
    past_fixes_section = ""
    if past_fixes:
        fixes_text = "\n\n".join(
            f"**Past issue**: {f['description']}\n**Confirmed fix**: {f['resolution']}"
            for f in past_fixes
        )
        past_fixes_section = f"\n\n### Confirmed Fixes from AgentOverflow\n{fixes_text}"

    rag_context = f"""## Nexus Product Context: {skill}

### Applicable Skills & Rules
{skills_md}

### Relevant Architecture Documentation
{confluence_chunks}

### Code Standards
{code_standards}{past_fixes_section}

### Important
You MUST format all code edits using SEARCH/REPLACE blocks as specified
in the system prompt. Do not use any other format."""

    rag_message = {"role": "system", "content": rag_context}

    # STEP 5: Assemble augmented messages (CRITICAL ORDER)
    augmented_messages = [
        aider_system_prompt,  # Index 0: aider's rules (UNTOUCHED)
        rag_message,          # Index 1: your RAG context
        *messages[1:]         # Index 2+: original user/assistant messages
    ]

    # STEP 6: Forward to LLM via Lumin8
    async def stream_response():
        try:
            async for chunk in lumin8_stream(
                model="claude-3-5-sonnet",  # or your preferred model
                messages=augmented_messages,
                temperature=body.get("temperature", 0),
                stream=True
            ):
                # chunk is already in OpenAI SSE format from Lumin8
                yield chunk  # includes "data: " prefix and "\n\n"
        except Exception as e:
            yield f'data: {{"error": "{str(e)}"}}\n\n'
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")
```

### Backend Sends (SSE Stream):

```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{"role":"assistant","content":"Here"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{"content":" is the fix:"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{"content":"\n\nsrc/validator.rs\n```rust\n<<<<<<< SEARCH\nfn validate_delegation() {\n    // old code\n}\n=======\nfn validate_delegation() {\n    // fixed code\n}\n>>>>>>> REPLACE\n```"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"nexus-agent","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]

```

### Client (CLI) Processes:

1. Receives SSE chunks
2. Extracts `content` from each chunk's delta
3. Streams to terminal (real-time feedback)
4. Parses final response for SEARCH/REPLACE blocks
5. Applies edits to local files using exact matching

---

## Why the Message Order is Critical

The CLI's file editing system **depends entirely** on the SEARCH/REPLACE format rules in the system prompt. If those rules are missing or pushed out of the attention window, the LLM will not produce correctly formatted edits.

### ❌ WRONG (Don't Do This):

```python
# Replacing aider's system prompt
messages[0] = {"role": "system", "content": my_custom_prompt}
# Result: LLM forgets SEARCH/REPLACE format → edits fail to apply

# Prepending RAG before aider's system prompt
messages.insert(0, {"role": "system", "content": rag_context})
# Result: aider's system prompt pushed to index 1 → lower attention priority

# Appending RAG to aider's system prompt content
messages[0]["content"] += "\n\n" + rag_context
# Result: RAG context pushed to end of very long system prompt → lower attention
```

### ✅ RIGHT (Do This):

```python
# Insert RAG as messages[1]
augmented = [
    messages[0],           # aider's system prompt (UNCHANGED)
    rag_message,          # your RAG context
    *messages[1:]         # user messages and chat history
]
# Result: Both systems get attention, SEARCH/REPLACE rules at start
```

---

## Agent Routing via Model Name

The CLI sends two different model names to the same backend URL. **The backend must route based on the `model` field in the request body:**

| `model` value | Route to | Response format required |
|---------------|----------|--------------------------|
| `nexus-agent` | Code agent | SEARCH/REPLACE blocks — **mandatory** |
| `nexus-architect` | Architect agent | Natural language plan only — **no SEARCH/REPLACE** |

### How `/architect` works end-to-end

```
User: /architect add input validation to the validator

Stage 1 — CLI sends:
  POST /v1/chat/completions
  {"model": "nexus-architect", "messages": [...]}

  Backend: routes to architect agent → returns a natural language plan:
  "Here's the plan: 1. Add None check at top of validate()..."

Stage 2 — CLI automatically sends (after user confirms "Edit the files?"):
  POST /v1/chat/completions
  {"model": "nexus-agent", "messages": [...plan text as user message...]}

  Backend: routes to code agent → returns SEARCH/REPLACE blocks → CLI applies file edits
```

⚠️ **Critical rule for `nexus-architect` responses**: Return a plain-text description of the changes. Do NOT include SEARCH/REPLACE blocks. The CLI treats the entire response as a plan and sends it to `nexus-agent` verbatim for the editing stage.

---

## API Endpoint Specifications

### 1. POST /v1/chat/completions

**Purpose**: OpenAI-compatible LLM endpoint with RAG injection. Routes to code or architect agent based on `model` field (see "Agent Routing via Model Name" above).

**Request Headers**:
- `X-Nexus-Skill` (required): Active product skill name
- `Content-Type: application/json`

**Authentication**: Backend authenticates via its own service account. No credentials required from CLI.

**Request Body**:
```json
{
  "model": "nexus-agent",
  "stream": true,
  "temperature": 0,
  "messages": [
    {"role": "system", "content": "...aider rules..."},
    {"role": "user", "content": "...user request..."}
  ]
}
```

**Response**: SSE stream (Content-Type: text/event-stream)
- Each chunk: `data: {json}\n\n`
- Terminator: `data: [DONE]\n\n`

**Errors**:
- 400: Invalid request
- 503: LLM service down

---

### 2. GET /api/skills

**Purpose**: List available product contexts

**Request Headers**:
- `X-Nexus-Skill` (optional): Current product context (for consistent logs)

**Response**:
```json
[
  {
    "name": "staking",
    "description": "Staking product - validator management and delegation",
    "keywords": ["stake", "validator", "delegation", "epoch"]
  },
  {
    "name": "payments",
    "description": "Payments processing and settlement",
    "keywords": ["payment", "settlement", "transaction", "ledger"]
  }
]
```

**Used By**: CLI skill auto-detection (on first run, matches keywords against repo metadata)

---

### 3. GET /api/skills/{name}

**Purpose**: Validate skill exists + return full content (for @skill override)

**Request Headers**: X-Nexus-Skill (optional): Current product context

**Response** (200):
```json
{
  "name": "staking",
  "description": "...",
  "skill_content": "# Staking Skills\n\n## Architecture\n..."
}
```

**Response** (404): Skill not found

**Used By**:
- CLI skill validation when user types `@skillname`
- Skill auto-detection to verify selection

---

### 4. POST /api/overflow/ingest

**Purpose**: Receive a developer query from `/solve` and **always return an answer immediately**.
The backend runs a semantic cache decision automatically — no human approval or staging required.

**Semantic cache decision** (fully automated):
- `similarity ≥ 0.87` → CACHE HIT: return stored answer, `cached=true`
- `0.65 ≤ sim < 0.87` → SIMILAR: call LLM, return answer, `persisted=false`
- `sim < 0.65` → NOVEL: call LLM, if `confidence ≥ 0.72` → auto-persist (`persisted=true`)

**Request Headers**: `X-Nexus-Skill` (optional): Scopes the similarity search to the same product

**Request Body** (rich context from CLI):
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
  "recent_messages": [{"role": "user", "content": "..."}]
}
```

**Response** (200):
```json
{
  "status": "ok",
  "issue_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "suggestion": "The token expiry check runs before the refresh call on line 47...",
  "cached": false,
  "cache_hit_similarity": null,
  "confidence_score": 0.82,
  "persisted": true
}
```

**`issue_id`**: The CLI stores this on the coder object (`_nexus_last_issue_id`). Used for auto-resolve on `/commit` and manual `/solved`. The `issue_id` references the session entry whether or not it was persisted.

**`suggestion`**: Always populated — either the cached answer (similarity ≥ 0.87) or the fresh LLM response. Never an empty string.

**`confidence_score`**: If < 0.60, the CLI surfaces a low-confidence warning to the developer.

**Used By**: CLI `/solve` command

---

### 5. POST /api/overflow/resolve

**Purpose**: Enrich a previously answered issue with the real committed fix. If the entry was
auto-persisted at ingest, it is **upgraded** in ChromaDB (AI answer → confirmed fix). If it
was session-only, it now becomes searchable for the first time with a 6-month TTL.

**Key design**: The CLI never asks the developer to write a description. Instead, it captures
`git show HEAD` at commit time and sends the raw diff. **The backend summarizes it via LLM.**
This means the KB always contains human-readable explanations even when commit messages say "fix".

**Request Headers**: `X-Nexus-Skill` (optional)

**Request Body** — one of two shapes:

```json
// Automatic path: commit-triggered, diff sent, backend summarizes
{
  "issue_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "committed_diff": "commit abc123\nAuthor: Dev <dev@co.com>\n\n    fix\n\ndiff --git a/auth/middleware.py ..."
}

// Manual path: developer explicitly typed /solved <note>
{
  "issue_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "resolution": "Moved TokenRefreshMiddleware to run before AuthMiddleware in the stack."
}
```

If both fields are present, `resolution` takes precedence (explicit > inferred).
If neither is present, return 422.

**Backend MUST**:
1. Look up the issue by `issue_id` — return 404 if not found
2. If `committed_diff` is present and `resolution` is absent — call the LLM with this prompt:
   ```
   An engineer was debugging this issue: {original_description}
   They committed the following changes: {committed_diff}
   In 1–2 sentences, explain what the fix was. Be specific about what changed and why it worked.
   ```
3. Store the final resolution text (LLM-generated or explicit) on the issue record
4. Re-embed `"Issue: {description}\nResolution: {resolution}"` and upsert into the vector index
5. Mark the issue as `resolved_at = now()`

**Response** (200):
```json
{
  "status": "resolved",
  "message": "Fix captured. The knowledge base will surface this for similar future issues."
}
```

**Response** (404): `issue_id` not found.
**Response** (422): Neither `committed_diff` nor `resolution` was provided.

**Used By**: CLI auto-triggers on `/commit` (sends diff). CLI also triggers on manual `/solved`.
After success, the CLI clears `_nexus_last_issue_id` to prevent stale re-use.

---

### 6. GET /v1/models

**Purpose**: Health check endpoint

**Response**:
```json
{
  "data": [
    {"id": "nexus-agent", "object": "model"}
  ]
}
```

**Used By**: CLI startup verification (ensures backend is running)

---

## AgentOverflow: Knowledge Base Design

AgentOverflow is the nexus-cli's developer knowledge system. It uses a **semantic cache**
to answer every `/solve` query instantly — either from the knowledge base (cache hit) or via
the LLM (cache miss). Confident, novel answers are automatically persisted with a 6-month
TTL. No human approval or staging area is required at any point.

### Lifecycle of an Issue

The key design principle: **developers provide zero extra input.** The backend decides
automatically what to cache; resolution is captured automatically from the git diff at
commit time if the developer does nothing, or from an explicit `/solved` note if they choose.

```
Developer types:    /solve Auth returns 403 after token refresh
                              │
                              ▼
CLI collects:       description + rich context (git diff, all_files, chat_files,
                    ident_mentions, file_mentions, lint/test outcomes, recent messages)
                              │
                              ▼
POST /api/overflow/ingest ────► backend runs SEMANTIC CACHE DECISION:
                              │
                              ├─ similarity ≥ 0.87  →  CACHE HIT
                              │     return stored answer instantly (no LLM call)
                              │     cached=true, persisted=false
                              │
                              ├─ 0.65 ≤ sim < 0.87  →  SIMILAR EXISTS
                              │     call LLM with similar entry as context
                              │     return answer, skip persist (dedup protection)
                              │     cached=false, persisted=false
                              │
                              └─ sim < 0.65  →  NOVEL QUERY
                                    call LLM (RAG + ranked file context)
                                    compute confidence score
                                    │
                                    ├─ confidence ≥ 0.72  →  AUTO-PERSIST
                                    │     store question + AI answer in vector DB
                                    │     6-month TTL, reset on each future cache hit
                                    │     cached=false, persisted=true
                                    │
                                    └─ confidence < 0.72  →  SERVE ONLY
                                          answer returned, nothing stored
                                          cached=false, persisted=false

                    CLI always prints the suggestion immediately.
                    Developer fixes the issue using suggestion or their own approach.
                              │
                    Developer types /commit  ← only thing they do
                              │
                              ▼
CLI auto-captures:  git show HEAD  (the committed diff — what actually changed)
                              │
                              ▼
POST /api/overflow/resolve ───► backend receives committed_diff
                                  ├─ calls LLM: "explain this fix in 1-2 sentences"
                                  │   given: original issue description + diff
                                  ├─ replaces AI answer with real-world fix in vector DB
                                  ├─ re-embeds (description + confirmed resolution)
                                  └─ resets TTL to 6 months from now

                    CLI prints one line: "📚 AgentOverflow: fix captured."
                    Developer sees nothing else. Zero extra steps.
```

**Why auto-persist at /solve time rather than waiting for /solved?**
Waiting for a confirmed resolution creates a chicken-and-egg problem: the cache is empty
until developers manually close loops, which they rarely do consistently. Auto-persisting
on confidence (≥ 0.72) means the KB populates from day one. The `/solved` path then enriches
entries that already exist — replacing AI speculation with real committed code. This gives
you both immediate utility and progressive quality improvement over time.

**Why the diff, not the commit message?**
Commit messages are almost never useful for a knowledge base — "fix", "wip", "changes",
"pr feedback" are the most common values in any real codebase. The diff is objective and
complete: it shows exactly which lines changed, in which files, to make the error stop.
Combined with the original issue description, the LLM can always produce a meaningful
1–2 sentence summary from a diff, even if the commit message is empty.

### Storage: ChromaDB + Relational DB

The backend already uses **ChromaDB** as its vector store. The AgentOverflow knowledge base
maps directly onto this: ChromaDB handles the embeddings and similarity search; your existing
SQLite database holds the raw records, diffs, and timestamps for structured queries.

**Two-store pattern:**

```
SQLite DB                          ChromaDB collection
─────────────────────────          ──────────────────────────────────────
overflow_issues table              "overflow_cache" collection
  id, skill, description,            One document per PERSISTED entry
  all_files, chat_files,             (auto-persisted at /solve OR enriched at /solved)
  solve_diff, committed_diff,        document = "Issue: ...\nAnswer/Resolution: ..."
  suggestion, resolution,            metadata = {skill, issue_id, expires_at}
  confidence, hit_count,             id = issue_id (same UUID)
  created_at, expires_at
```

SQLite is the source of truth for all records.
ChromaDB contains **persisted entries** — entries where the backend decided to cache the
answer (auto-persisted at ingest) or entries subsequently enriched via `/solved`.
Session-only entries (served but not persisted) exist only in SQLite for `/solved` tracking.

**Relational schema (SQLite):**

```sql
CREATE TABLE IF NOT EXISTS overflow_issues (
    id              TEXT PRIMARY KEY,        -- UUID generated in Python: str(uuid.uuid4())
    skill           TEXT NOT NULL,
    description     TEXT NOT NULL,           -- developer's /solve message
    all_files       TEXT,                    -- JSON: coder.get_all_relative_files()
    chat_files      TEXT,                    -- JSON: explicitly /add'd files
    ident_mentions  TEXT,                    -- JSON: identifiers from description
    file_mentions   TEXT,                    -- JSON: filenames from description
    solve_diff      TEXT,                    -- git diff at /solve time
    committed_diff  TEXT,                    -- git show HEAD captured at /commit
    suggestion      TEXT,                    -- AI answer (may be replaced by resolution)
    resolution      TEXT,                    -- confirmed fix (NULL = not yet enriched)
    confidence      REAL,                    -- LLM confidence 0-1 at ingest time
    hit_count       INTEGER DEFAULT 0,       -- cache hit count (for analytics)
    persisted       INTEGER DEFAULT 0,       -- 1 = in ChromaDB, 0 = session-only
    created_at      TEXT DEFAULT (datetime('now')),
    expires_at      TEXT                     -- 6-month flat TTL, NULL = session-only (not in ChromaDB)
);
```

SQLite notes vs Postgres:
- No `UUID` type → use `TEXT`, generate with `str(uuid.uuid4())` in Python
- No array type → store `files` as `json.dumps(list)`, read back with `json.loads()`
- No `now()` → use `datetime('now')` in SQL or `datetime.utcnow().isoformat()` in Python
- Placeholders are `?` (positional) or `:name` (named), not `$1`
- Use `sqlite3` (stdlib) or `aiosqlite` for async access

**ChromaDB collection setup:**

```python
import chromadb

# Use PersistentClient in production so data survives restarts
chroma_client = chromadb.PersistentClient(path="./chroma_data")

# One collection for all AgentOverflow resolutions
# cosine distance is best for semantic text similarity
overflow_collection = chroma_client.get_or_create_collection(
    name="overflow_cache",
    metadata={"hnsw:space": "cosine"},
)
```

ChromaDB handles its own embeddings internally (using its default embedding function,
or you can plug in your own). If Lumin8 exposes an `/embeddings` endpoint, you can
wire it in as a custom embedding function — otherwise the default (`all-MiniLM-L6-v2`)
works well for code-related text.

### Ingest Flow (POST /api/overflow/ingest)

At ingest time, the backend runs the semantic cache decision and **always returns an answer
immediately**. If the answer is confident and novel, it is auto-persisted to ChromaDB with
a 6-month TTL — no human approval or staging step required.

```python
import sqlite3, uuid, json
from datetime import datetime, timedelta

# Open (or create) the SQLite database — one file, zero extra infrastructure
db = sqlite3.connect("nexus_overflow.db", check_same_thread=False)
db.row_factory = sqlite3.Row   # lets you access columns by name: row["description"]
db.execute("""
    CREATE TABLE IF NOT EXISTS overflow_issues (
        id TEXT PRIMARY KEY, skill TEXT NOT NULL, description TEXT NOT NULL,
        all_files TEXT, chat_files TEXT, ident_mentions TEXT, file_mentions TEXT,
        solve_diff TEXT, committed_diff TEXT, suggestion TEXT, resolution TEXT,
        confidence REAL, hit_count INTEGER DEFAULT 0, persisted INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')), expires_at TEXT
    )
""")
db.commit()


CACHE_HIT_THRESHOLD  = 0.87   # return cached answer, skip LLM
SIMILAR_THRESHOLD    = 0.65   # call LLM but skip persist (dedup protection)
CONFIDENCE_THRESHOLD = 0.72   # minimum confidence to auto-persist novel answers
DEDUP_THRESHOLD      = 0.80   # secondary dedup gate before storing
TTL_DAYS             = 180    # flat 6-month TTL for all persisted entries


def handle_ingest(body: dict, skill: str) -> dict:
    description    = body["description"]
    git_diff       = body.get("git_diff", "")
    all_files      = body.get("all_files", [])
    chat_files     = body.get("chat_files", [])
    ident_mentions = body.get("ident_mentions", [])
    file_mentions  = body.get("file_mentions", [])
    lint_outcome   = body.get("lint_outcome")
    test_outcome   = body.get("test_outcome")

    issue_id = str(uuid.uuid4())

    # ── 1. Embed description (+ ident_mentions as boosting context) ────────────
    query_text = description
    if ident_mentions:
        query_text += " " + " ".join(ident_mentions[:5])

    # ── 2. Similarity search against ChromaDB (persisted entries only) ─────────
    similar = search_overflow(query=query_text, skill=skill, limit=1)
    best_sim = similar[0]["score"] if similar else 0.0
    cached = False
    persisted = False
    confidence_score = None
    cache_hit_similarity = None

    if best_sim >= CACHE_HIT_THRESHOLD:
        # ── CACHE HIT ──────────────────────────────────────────────────────────
        cached = True
        cache_hit_similarity = round(best_sim, 3)
        suggestion = similar[0]["suggestion"]
        # Update hit count and reset TTL in SQLite + ChromaDB
        db.execute(
            "UPDATE overflow_issues SET hit_count = hit_count + 1, expires_at = ? WHERE description = ?",
            ((datetime.utcnow() + timedelta(days=TTL_DAYS)).isoformat(), similar[0]["description"])
        )
        db.commit()
    else:
        # ── LLM PATH ───────────────────────────────────────────────────────────
        # Rank files by relevance (chat_files → 50× boost, ident/file mentions → 10×)
        ranked_files = rank_by_relevance(all_files, chat_files, ident_mentions, file_mentions)

        # Retrieve top KB entries as RAG context
        kb_entries = search_overflow(query=query_text, skill=skill, limit=3, min_score=0.55)

        # Build LLM prompt with enriched context
        suggestion = call_llm_with_rag(description, ranked_files, kb_entries, skill)

        # ── Confidence signal ─────────────────────────────────────────────────
        confidence_score = compute_confidence(suggestion, lint_outcome, test_outcome)

        novel = best_sim < SIMILAR_THRESHOLD
        dedup_clear = best_sim < DEDUP_THRESHOLD  # secondary check before storing

        if novel and dedup_clear and confidence_score >= CONFIDENCE_THRESHOLD:
            # AUTO-PERSIST: no human approval required
            persisted = True
            expires_at = (datetime.utcnow() + timedelta(days=TTL_DAYS)).isoformat()

            # Store in SQLite (source of truth)
            db.execute(
                """INSERT INTO overflow_issues
                   (id, skill, description, all_files, chat_files, ident_mentions,
                    file_mentions, solve_diff, suggestion, confidence, persisted, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
                (issue_id, skill, description,
                 json.dumps(all_files), json.dumps(chat_files),
                 json.dumps(ident_mentions), json.dumps(file_mentions),
                 git_diff, suggestion, confidence_score, expires_at),
            )
            db.commit()

            # Add to ChromaDB (now searchable)
            doc_text = f"Issue: {description}\nAnswer: {suggestion}"
            overflow_collection.upsert(
                ids=[issue_id],
                documents=[doc_text],
                metadatas=[{"skill": skill, "description": description,
                             "suggestion": suggestion, "expires_at": expires_at}],
            )
        else:
            # SESSION-ONLY: serve the answer, don't cache
            db.execute(
                """INSERT INTO overflow_issues
                   (id, skill, description, solve_diff, suggestion, confidence, persisted)
                   VALUES (?,?,?,?,?,?,0)""",
                (issue_id, skill, description, git_diff, suggestion, confidence_score),
            )
            db.commit()
            # Not added to ChromaDB — may be enriched via /solved later

    return {
        "status": "ok",
        "issue_id": issue_id,
        "suggestion": suggestion,
        "cached": cached,
        "cache_hit_similarity": cache_hit_similarity,
        "confidence_score": confidence_score,
        "persisted": persisted,
    }
```

### Resolve Flow (POST /api/overflow/resolve)

This is where real-world confirmation enriches the knowledge base. The committed diff
comes in, the LLM summarizes it, and the stored entry is **upgraded** — replacing the
original AI answer with a team-verified fix. If the entry was already in ChromaDB
(auto-persisted at ingest), it is updated in-place. If it was session-only, it now
becomes searchable for the first time.

```python
async def generate_resolution_summary(description: str, committed_diff: str) -> str:
    """
    Use the LLM (via Lumin8) to turn a raw git diff into a human-readable fix summary.
    This is why AgentOverflow works without asking developers to write anything —
    the diff IS the evidence; the LLM just makes it readable.
    """
    prompt = f"""An engineer was debugging this issue:
{description}

They committed the following changes to fix it:
{committed_diff[:4000]}

In 1-2 sentences, explain what the fix was. Be specific: name the files, functions,
or lines that changed and explain why that resolved the issue.
Do not restate the problem — only describe the solution."""

    response = await lumin8_client.chat.completions.create(
        model="nexus-agent",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        stream=False,
    )
    return response.choices[0].message.content.strip()


def handle_resolve(issue_id: str, committed_diff: str = None, resolution: str = None):
    # Fetch the raw record from SQLite
    row = db.execute(
        "SELECT * FROM overflow_issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Issue {issue_id!r} not found.")
    if not committed_diff and not resolution:
        raise HTTPException(status_code=422, detail="Provide committed_diff or resolution.")

    # Generate the human-readable resolution text
    if resolution:
        final_resolution = resolution          # explicit developer note — use verbatim
    else:
        # generate_resolution_summary is async (calls Lumin8); call with asyncio if needed
        final_resolution = generate_resolution_summary(row["description"], committed_diff)

    # Update SQLite — set resolution, committed_diff, and resolved_at timestamp
    db.execute("""
        UPDATE overflow_issues
        SET resolution = ?, committed_diff = ?, resolved_at = datetime('now')
        WHERE id = ?
    """, (final_resolution, committed_diff, issue_id))
    db.commit()

    # Update SQLite — set resolution, committed_diff, expires_at (reset TTL), mark persisted
    new_expires_at = (datetime.utcnow() + timedelta(days=TTL_DAYS)).isoformat()
    db.execute("""
        UPDATE overflow_issues
        SET resolution = ?, committed_diff = ?, suggestion = ?, persisted = 1,
            expires_at = ?
        WHERE id = ?
    """, (final_resolution, committed_diff, final_resolution, new_expires_at, issue_id))
    db.commit()

    # Upsert to ChromaDB — enriched document with confirmed fix replaces AI speculation.
    # `upsert` handles both cases: entry already in ChromaDB (update) or new (insert).
    doc_text = f"Issue: {row['description']}\nResolution: {final_resolution}"
    overflow_collection.upsert(
        ids=[issue_id],
        documents=[doc_text],
        metadatas=[{
            "skill": row["skill"],
            "description": row["description"],
            "suggestion": final_resolution,
            "expires_at": new_expires_at,
        }],
    )
```

**Why `upsert` instead of `add`?** If `/solved` is called twice for the same issue (e.g.
the developer refines their note), `upsert` replaces the existing document cleanly.
It also handles the case where the entry was already in ChromaDB from auto-persist at
ingest — `upsert` upgrades it with the real fix.

**Why combine description + resolution as one document string?** ChromaDB embeds the
`document` field. Future ingest queries come in as issue descriptions — e.g. "getting 403
on auth endpoints". You want that query to hit the stored "Issue: token refresh causes 403 /
Resolution: moved refresh middleware before auth check" record. Including both sides in
the document shifts the embedding toward the solution space, not just the problem space,
which dramatically improves retrieval quality.

### Retrieval at Chat Time (and at Ingest)

Used in two places: (1) at ingest to surface a suggestion immediately; (2) at every
`/v1/chat/completions` request to inject past fixes into `messages[1]`.

```python
async def search_overflow(query: str, skill: str, limit: int = 3) -> list[dict]:
    """
    Query ChromaDB for the most similar resolved issues.
    Only resolved issues exist in ChromaDB, so no extra filter needed.
    The `skill` metadata filter scopes results to the same product context.
    Returns [] if ChromaDB is unavailable — never blocks the LLM.
    """
    try:
        results = overflow_collection.query(
            query_texts=[query],
            n_results=limit,
            where={"skill": skill},          # filter to same product context
        )
        # ChromaDB returns parallel lists — zip them up
        docs      = results["documents"][0]   # list of document strings
        metadatas = results["metadatas"][0]   # list of metadata dicts
        distances = results["distances"][0]   # cosine distances (lower = more similar)

        # Filter out weak matches (distance > 0.25 ≈ cosine similarity < 0.75)
        # Tune this threshold based on your data — start at 0.25 and adjust
        return [
            {
                "description": meta["description"],
                "resolution":  meta["resolution"],
                "score": 1 - dist,            # convert distance to similarity for readability
            }
            for meta, dist in zip(metadatas, distances)
            if dist < 0.25
        ]
    except Exception:
        return []   # ChromaDB down → degrade gracefully, never block the LLM
```

**Threshold note**: ChromaDB with cosine distance returns values from 0 (identical) to 2
(opposite). A distance of `0.25` corresponds to roughly 0.75 cosine similarity. Start there
and tune — too low means noisy suggestions, too high means nothing is ever retrieved.

The results are injected into the RAG context at `messages[1]`:

```python
past_fixes = await search_overflow(query=messages[-1]["content"], skill=skill)
if past_fixes:
    fixes_section = "\n\n".join(
        f"**Past issue**: {f['description']}\n**Confirmed fix**: {f['resolution']}"
        for f in past_fixes
    )
    rag_context += f"\n\n### Confirmed Fixes from Developer Knowledge Base\n{fixes_section}"
```

The LLM sees real, team-verified fixes from your own codebase alongside SKILLS.md and
Confluence — giving it grounded, product-specific answers instead of generic advice.

### What the CLI Does NOT Do

The CLI has no vector DB, no embedding logic, and no knowledge storage. It only:

1. Sends `POST /api/overflow/ingest` with rich context (description, git_diff, all_files, chat_files, ident_mentions, file_mentions, lint/test outcomes, recent messages) → gets back `{issue_id, suggestion, cached, confidence_score, persisted}`
2. Stores `issue_id` on the coder object (`self.coder._nexus_last_issue_id`)
3. Sends `POST /api/overflow/resolve` with `issue_id + committed_diff` (auto on /commit) or `issue_id + resolution` (manual /solved) → gets back confirmation
4. Clears the stored `issue_id` after a successful resolve call

All embedding, similarity search, LLM calls, confidence scoring, and storage decisions are the **backend's responsibility**. The CLI never knows or cares whether a response was cached or freshly generated — it just shows the suggestion.

All embedding, search, and storage is the backend's responsibility.

---

### Authentication

- [ ] Backend authenticates via its own service account (not from CLI)
- [ ] CLI sends NO auth headers (they're not required)
- [ ] Extract `X-Nexus-Skill` header to determine product context
- [ ] (Optional) Log X-Nexus-Skill for audit trails

### Skill Management

- [ ] Load skills from database/config (name, description, keywords, content)
- [ ] Store SKILLS.md files per product (e.g., `STAKING_SKILLS.md`)
- [ ] `/api/skills` returns all available skills with keywords
- [ ] `/api/skills/{name}` returns full skill metadata + content
- [ ] Handle 404 if skill not found

### Chat Completions (CRITICAL)

- [ ] Receive SSE streaming from Lumin8 for LLM model
- [ ] **DO NOT MODIFY messages[0]** (aider's system prompt)
- [ ] Load SKILLS.md for `X-Nexus-Skill`
- [ ] Search Confluence for relevant chunks (optional but recommended)
- [ ] Load code standards for the skill
- [ ] Build RAG context message with SKILLS.md + Confluence + standards
- [ ] **INSERT RAG at messages[1]** (after aider's prompt)
- [ ] Forward augmented messages to LLM
- [ ] Stream response back as SSE chunks
- [ ] Format: `data: {json}\n\n` for each chunk
- [ ] Terminator: `data: [DONE]\n\n`

### AgentOverflow Knowledge Base

- [ ] Create `overflow_issues` table in SQLite (schema in KB Design section above — includes `all_files`, `chat_files`, `ident_mentions`, `file_mentions`, `confidence`, `hit_count`, `persisted`, `expires_at`)
- [ ] Create ChromaDB `overflow_cache` collection with `{"hnsw:space": "cosine"}`
- [ ] `POST /api/overflow/ingest`:
  - [ ] Accept all rich context fields: `description`, `git_diff`, `dirty_files`, `recent_commits`, `all_files`, `chat_files`, `ident_mentions`, `file_mentions`, `lint_outcome`, `test_outcome`, `recent_messages`
  - [ ] Embed `description` (+ `ident_mentions` as boosting context)
  - [ ] Run similarity search: ≥ 0.87 = cache hit, 0.65-0.87 = similar (serve, skip persist), < 0.65 = novel
  - [ ] For LLM calls: rank files by relevance (chat_files 50×, ident/file mentions 10×), inject as RAG
  - [ ] Compute confidence score from response length, code blocks, test/lint outcome
  - [ ] Auto-persist novel + confident (≥ 0.72) answers: upsert to ChromaDB + SQLite with 6-month TTL
  - [ ] Return `{ status, issue_id, suggestion, cached, cache_hit_similarity, confidence_score, persisted }`
- [ ] `POST /api/overflow/resolve`:
  - [ ] Look up `issue_id` in SQLite; return 404 if not found
  - [ ] If `resolution` present: use it directly
  - [ ] If only `committed_diff` present: call LLM to summarize → use as `resolution`
  - [ ] If neither: return 422
  - [ ] Update SQLite: set `resolution`, `committed_diff`, `persisted=1`, reset `expires_at` to +6 months
  - [ ] `overflow_collection.upsert(id=issue_id, document="Issue: ...\nResolution: ...", metadata={skill, expires_at, ...})`
  - [ ] Return `{ status: "resolved", message }`
- [ ] At chat time (`/v1/chat/completions`), call `search_overflow()` and inject results into `messages[1]`

### Health Check

- [ ] `/v1/models` returns minimal model list
- [ ] Used by CLI at startup to verify backend is running

---

## Confluence Integration (Optional but Recommended)

The backend can enhance responses by searching Confluence for relevant documentation. Suggested flow:

```python
def search_confluence(query: str, limit: int = 5) -> str:
    """
    Search Confluence for docs related to the user's request.

    Args:
        query: User's message (e.g., "Fix the delegation bug in staking")
        limit: Max number of results to return

    Returns:
        Formatted string of relevant Confluence pages/sections
    """
    # 1. Connect to Confluence API
    confluence = Confluence(
        url=CONFLUENCE_URL,
        username=CONFLUENCE_USER,
        password=CONFLUENCE_TOKEN
    )

    # 2. Search for relevant pages
    results = confluence.cql(
        f'space = "STAKING" AND text ~ "{query}" ORDER BY lastModified DESC',
        limit=limit
    )

    # 3. Extract key sections
    chunks = []
    for page in results:
        title = page.get("title", "")
        body = page.get("body", {}).get("storage", {}).get("value", "")
        # Clean HTML, extract key sections, limit length
        chunks.append(f"## {title}\n{clean_html(body)}")

    return "\n\n".join(chunks)
```

---

## Lumin8 Integration

Lumin8 is your LLM abstraction layer. It should provide a streaming interface compatible with OpenAI's chat completions format.

```python
async def lumin8_stream(model: str, messages: list, **kwargs):
    """
    Stream from Lumin8 LLM router.

    Expected to yield SSE-formatted chunks.
    """
    # Lumin8 client initialization
    lumin8_client = Lumin8Client(api_key=LUMIN8_KEY)

    # Stream completion
    async with lumin8_client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        **kwargs
    ) as stream:
        async for chunk in stream:
            # Lumin8 should return OpenAI format
            # If not, you may need to transform here
            yield chunk
```

---

## Error Handling & Graceful Degradation

### Lumin8/LLM Service Down
→ Return 503, suggest checking LLM status

### Confluence Unavailable
→ Continue without Confluence chunks, inject what you have

### Skill Not Found
→ Return 404 or default to "default" skill

### AgentOverflow issue_id Not Found
→ Return 404 with a clear message — issue may have expired or been already resolved

### Vector DB / Embedding Service Down
→ Continue without past-fix context; log the error but don't block the LLM response

### Message Malformed
→ Return 400 with error details

---

## Testing the Integration

### 1. Start the Mock Backend

```bash
cd /path/to/nexus-cli
python3 mock_backend.py
```

This runs a simplified backend that echoes requests and streams mock responses. Use it to verify the CLI sends correct headers and messages.

### 2. Verify Request Structure

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "X-Nexus-Skill: staking" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nexus-agent",
    "stream": true,
    "messages": [
      {"role": "system", "content": "Test system prompt with SEARCH/REPLACE rules"},
      {"role": "user", "content": "Fix the bug"}
    ]
  }' | head -20
```

### 3. Test Each Endpoint

```bash
# List skills
curl http://localhost:8000/api/skills

# Get single skill
curl http://localhost:8000/api/skills/staking

# Submit error — note the issue_id in the response
curl -X POST http://localhost:8000/api/overflow/ingest \
  -H "Content-Type: application/json" \
  -H "X-Nexus-Skill: staking" \
  -d '{"description":"Auth 403 after token refresh","git_diff":"","all_files":["src/auth.py","src/middleware.py"],"chat_files":["src/auth.py"],"ident_mentions":["AuthMiddleware"],"file_mentions":["auth.py"]}'
# → {"status":"ok","issue_id":"<uuid>","suggestion":"...","cached":false,"confidence_score":0.82,"persisted":true}

# Record the confirmed fix (use the issue_id from above)
curl -X POST http://localhost:8000/api/overflow/resolve \
  -H "Content-Type: application/json" \
  -d '{"issue_id":"<uuid>","resolution":"Moved token refresh to run before auth middleware."}'
# → {"status":"resolved","message":"..."}

# Health check
curl http://localhost:8000/v1/models
```

### 4. Full End-to-End

```bash
# Install nexus-cli
cd /path/to/nexus-cli
pip install -e .

# Run CLI (skill detection runs automatically)
nexus

# Inside the CLI session:
# /solve Auth middleware returns 403 after token refresh
# → CLI calls /api/overflow/ingest, prints suggestion + stores issue_id
#
# (Try the suggestion, find the real fix...)
#
# /solved Moving token refresh middleware earlier in the stack fixed it
# → CLI calls /api/overflow/resolve with stored issue_id
# → Knowledge base now records this fix for future developers
```

---

## Performance & Optimization Tips

1. **Confluence Caching**: Cache Confluence search results for 1-5 minutes (results rarely change mid-session)
2. **Skill Metadata Caching**: Load skill list on server startup, refresh on a background timer
3. **Overflow KB Caching**: Cache top-K results from the vector DB for common queries (LRU, short TTL)
4. **SSE Chunking**: Send smaller chunks for more responsive terminal UI
5. **LLM Timeout**: Set a reasonable timeout (e.g., 5 min) for long responses — aider sessions can be verbose
6. **Connection Pooling**: Reuse HTTP connections to Lumin8, Confluence, and the vector DB
7. **Embedding Batching**: When ingesting issues, batch embedding calls if multiple arrive simultaneously

---

## Common Pitfalls

❌ **Modifying messages[0]**: Don't change aider's system prompt
❌ **Appending RAG to messages[0]**: Pushes SEARCH/REPLACE rules out of attention
❌ **Not streaming**: Always return SSE stream for responsiveness
❌ **Wrong SSE format**: Must be `data: {json}\n\n` (two newlines)
❌ **Missing error handling**: Always return proper HTTP status codes
❌ **Hardcoding model names**: Use Lumin8's model registry

---

## Questions for Clarification

Before implementation, confirm:

1. **Lumin8 Integration**: How does Lumin8 provide streaming? (async generator? WebSocket? OpenAI-compatible SDK?)
2. **Confluence**: Is Confluence available? What auth method and space key(s) should be searched?
3. **Skills Storage**: Database? Filesystem? Config service? (Influences how SKILLS.md files are loaded per product)
4. **Embedding model for ChromaDB**: ChromaDB defaults to `all-MiniLM-L6-v2`. If Lumin8 exposes an `/embeddings` endpoint, wire it in as a custom embedding function for consistency across the platform.
5. **AgentOverflow Retention**: How long should unresolved issues be kept before expiry?
6. **Error Logging**: Centralised logging service? (Datadog, Splunk, etc.) — needed for observability around AgentOverflow usage

---

## Additional Resources

- **OpenAPI Spec**: `docs/nexus-backend-openapi.yaml`
- **Integration Guide**: `docs/nexus-backend-integration-guide.md`
- **Mock Backend**: `mock_backend.py` (runnable reference)
- **CLI Source**: `aider/` directory (study how it handles responses)
- **Test Integration**: `test_integration.py` (verifies key code paths)

---

## Ready to Implement

You now have:

✅ Complete request/response flow with examples
✅ Message ordering rules (CRITICAL)
✅ API endpoint specifications (including /api/overflow/resolve)
✅ AgentOverflow knowledge base design (lifecycle, schema, retrieval, embedding strategy)
✅ Implementation checklist
✅ Error handling guidance
✅ Testing instructions with curl examples
✅ Performance tips
✅ Common pitfalls to avoid

**Start with**: Health check → Chat completions → Skills → AgentOverflow ingest → AgentOverflow resolve

**Test with**: Mock backend → CLI with real backend

**Deploy to**: Internal network alongside Lumin8

Good luck! 🚀
