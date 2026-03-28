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
│  3. AgentOverflow Knowledge Base                            │
│     ├─ /api/overflow/ingest  — store issue + return id     │
│     │   └─ similarity search → surface past resolutions    │
│     └─ /api/overflow/resolve — attach confirmed fix to id  │
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

**Purpose**: Receive error/debugging submissions from the `/solve` CLI command; run similarity
search to surface any past confirmed resolutions; return an `issue_id` so the developer can
later call `/solved` to close the loop.

**Request Headers**: `X-Nexus-Skill` (optional): Scopes similarity search to the same product

**Request Body**:
```json
{
  "description": "Auth middleware returns 403 after token refresh",
  "git_diff": "diff --git a/auth.py b/auth.py\n...",
  "files_in_context": ["src/auth.py", "src/middleware.py", "tests/auth_test.py"]
}
```

**Response** (200):
```json
{
  "status": "ingested",
  "issue_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "suggestion": "Similar past issue resolved with: Moving token refresh before auth check fixed the 403s."
}
```

**`issue_id`**: The CLI stores this on the coder object (`_nexus_last_issue_id`). When the
developer runs `/solved <explanation>`, the CLI sends `issue_id + resolution` to the resolve
endpoint to permanently attach the confirmed fix to this issue record.

**`suggestion`**: May be empty string if no close match was found in the knowledge base.

**Used By**: CLI `/solve` command

---

### 5. POST /api/overflow/resolve

**Purpose**: Attach a confirmed, developer-verified resolution to a previously ingested issue.
This is the "close the loop" step — it's what actually builds the knowledge base.

**Request Headers**: `X-Nexus-Skill` (optional)

**Request Body**:
```json
{
  "issue_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "resolution": "The token refresh middleware was running after the auth check. Moving it earlier in the middleware stack fixed the 403s."
}
```

**Response** (200):
```json
{
  "status": "resolved",
  "message": "Resolution recorded. This fix will be surfaced for similar future issues."
}
```

**Response** (404): `issue_id` not found or already resolved.

**Used By**: CLI `/solved` command. After this call succeeds, the CLI clears its locally
stored `_nexus_last_issue_id` to prevent stale re-use.

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

AgentOverflow is the nexus-cli's developer knowledge system. It transforms each debugging
session into a permanent, searchable record so that the next developer who hits the same
error gets an immediate, battle-tested fix — without needing to ask the LLM to rediscover it.

### Lifecycle of an Issue

```
Developer types:    /solve Auth returns 403 after token refresh
                              │
                              ▼
CLI collects:       description + git diff + files_in_context
                              │
                              ▼
POST /api/overflow/ingest ────► backend assigns issue_id
                              │   ├─ embeds description
                              │   ├─ similarity search → top-K past resolutions
                              │   ├─ returns issue_id + suggestion (may be empty)
                              │   └─ stores pending issue in DB
                              │
                    CLI prints suggestion (or "No similar issues found")
                    CLI stores issue_id on coder object
                              │
                    Developer tries the fix (or figures out their own)
                              │
Developer types:    /solved Moving token refresh before auth check fixed it
                              │
                              ▼
POST /api/overflow/resolve ───► backend attaches resolution to issue_id
                                  ├─ re-embeds (description + resolution) together
                                  ├─ updates vector index
                                  └─ marks issue as resolved
```

### Database Schema (Suggested)

```sql
-- One row per submitted issue
CREATE TABLE overflow_issues (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill       TEXT NOT NULL,                  -- X-Nexus-Skill at time of submission
    description TEXT NOT NULL,                  -- User's /solve message
    git_diff    TEXT,                           -- git diff HEAD output
    files       TEXT[],                         -- files in aider context
    resolution  TEXT,                           -- populated by /resolve; NULL = unresolved
    embedding   VECTOR(1536),                   -- embed(description + resolution) once resolved
                                                -- embed(description) alone while unresolved
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

-- Index for fast similarity search (pgvector)
CREATE INDEX ON overflow_issues USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

If you are not using pgvector, the same pattern works with Qdrant, Pinecone, or any
vector store: store one vector per issue, include `skill` as a metadata filter field.

### Embedding Strategy

**At ingest time** (issue pending, no resolution yet):
```python
embedding = embed(issue.description)
# This lets us do similarity search immediately, even before the fix is known.
```

**At resolve time** (confirmed fix attached):
```python
# Re-embed with resolution included — this is what will be retrieved in future
embedding = embed(f"{issue.description}\n\nResolution: {issue.resolution}")
issue.resolved_at = now()
vector_db.upsert(id=issue.id, vector=embedding, metadata={"skill": issue.skill, "resolved": True})
```

**Why combine description + resolution in the final embedding?** Because future queries
come from new issue descriptions. You want semantic overlap between "token refresh causes 403"
(new query) and "Auth returns 403 after refresh — fix: move middleware earlier" (stored record).
The resolution text anchors the meaning more precisely than the description alone.

### Retrieval at Chat Time

Every `/v1/chat/completions` request can optionally inject past fixes into `messages[1]`.
Only retrieve **resolved** issues (those with a non-null `resolution`).

```python
def search_overflow(query: str, skill: str, limit: int = 3) -> list[dict]:
    """
    Find the top-K most similar resolved AgentOverflow issues for the current query.

    Args:
        query: The user's message (e.g. "Fix the delegation bug in staking contract")
        skill: Active product skill (for optional pre-filter)
        limit: Max number of past fixes to inject

    Returns:
        List of {"description": str, "resolution": str} dicts, most similar first.
        Returns [] if the vector DB is unavailable (graceful degradation).
    """
    try:
        query_vec = embed(query)
        results = vector_db.search(
            vector=query_vec,
            filter={"skill": skill, "resolved": True},  # only confirmed fixes
            top_k=limit,
            min_score=0.82,   # tune this — too low = noise, too high = misses
        )
        return [
            {"description": r.metadata["description"], "resolution": r.metadata["resolution"]}
            for r in results
        ]
    except Exception:
        return []   # never let KB failures block the LLM
```

The results are injected into the RAG context block at `messages[1]`:

```python
if past_fixes:
    fixes_section = "\n\n".join(
        f"**Past issue**: {f['description']}\n**Confirmed fix**: {f['resolution']}"
        for f in past_fixes
    )
    rag_context += f"\n\n### Confirmed Fixes from Developer Knowledge Base\n{fixes_section}"
```

The LLM then sees real confirmed fixes from your team's own codebase alongside the SKILLS.md
and Confluence context — giving it grounded, product-specific answers instead of generic advice.

### Similarity Threshold Tuning

The `min_score` of `0.82` (cosine similarity) is a starting point. Calibrate it by:

1. Collecting 20–30 representative issue descriptions from developers
2. Manually labelling which pairs are "same problem" vs "different problem"
3. Finding the score that best separates the two classes

A score too low causes noisy suggestions ("here's a database fix for your auth problem").
A score too high causes missed retrievals and the system looks useless. `0.78–0.85` is a
typical sweet spot for code-related issue embeddings.

### Recommended Embedding Model

Use the same embedding model the rest of the platform uses if one exists. Otherwise:
- **`text-embedding-3-small`** (OpenAI) — 1536 dimensions, fast, cheap
- **`text-embedding-ada-002`** — legacy but widely deployed
- If using Lumin8 as the LLM router, check if it exposes an `/embeddings` endpoint

### What the CLI Does NOT Do

The CLI has no vector DB, no embedding logic, and no knowledge storage. It only:

1. Sends `POST /api/overflow/ingest` with description + context → gets back `issue_id + suggestion`
2. Stores `issue_id` on the coder object (`self.coder._nexus_last_issue_id`)
3. Sends `POST /api/overflow/resolve` with `issue_id + resolution` → gets back confirmation
4. Clears the stored `issue_id` after a successful `/solved` call

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

- [ ] `POST /api/overflow/ingest`:
  - [ ] Accept `description`, `git_diff`, `files_in_context`
  - [ ] Assign a UUID `issue_id` and persist the issue record
  - [ ] Run similarity search against existing *resolved* issues (same skill preferred)
  - [ ] Return `{ status, issue_id, suggestion }` — suggestion empty string if no match
- [ ] `POST /api/overflow/resolve`:
  - [ ] Look up `issue_id`; return 404 if not found
  - [ ] Attach `resolution` text to the issue record
  - [ ] Re-embed combined `(description + resolution)` and update the vector index
  - [ ] Return `{ status: "resolved", message }`
- [ ] At chat time (`/v1/chat/completions`), query overflow KB for past fixes and inject into `messages[1]`

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
  -d '{"description":"Auth 403 after token refresh","git_diff":"","files_in_context":["src/auth.py"]}'
# → {"status":"ingested","issue_id":"<uuid>","suggestion":"..."}

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
4. **Vector DB**: What embedding model and vector store are available? (pgvector in Postgres? Pinecone? Qdrant?)
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
