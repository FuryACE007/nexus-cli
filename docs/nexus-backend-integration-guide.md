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

All requests include these headers:

| Header | Description |
|--------|-------------|
| `X-Internal-Auth-Token` | internal auth proxy username |
| `X-Internal-Auth-Secret` | internal auth proxy password |
| `X-Nexus-Skill` | Active product context name (on `/v1/chat/completions` only) |

The backend should validate credentials against the internal auth proxy on each request (or use a session/token cache internally).

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

**Purpose**: Receive error/issue submissions from `/solve` command

**Request Body**:
```json
{
  "description": "Auth middleware returns 403 after token refresh",
  "git_diff": "diff --git a/auth.py b/auth.py\n...",
  "files_in_context": ["src/auth.py", "src/middleware.py"]
}
```

**Response**:
```json
{
  "status": "ingested",
  "suggestion": "Optional immediate debugging suggestion"
}
```

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
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import json

app = FastAPI(title="Nexus Backend", version="1.0.0")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body["messages"]
    skill = request.headers.get("X-Nexus-Skill", "default")
    username = request.headers.get("X-Internal-Auth-Token")
    password = request.headers.get("X-Internal-Auth-Secret")

    # TODO: Authenticate via internal auth proxy
    # TODO: Load SKILLS.md for the active skill
    # TODO: Search Confluence for relevant chunks based on user message
    # TODO: Insert RAG context as messages[1] (see integration guide)
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
    # TODO: Store/analyze the error submission
    return {"status": "ingested", "suggestion": ""}


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
