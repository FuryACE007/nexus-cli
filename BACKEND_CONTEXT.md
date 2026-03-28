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
                         │ HTTP/HTTPS + TLS
                         │
┌────────────────────────▼────────────────────────────────────┐
│ Nexus FastAPI Backend (localhost:8000)                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  1. Authentication Layer                                     │
│     └─ Backend service account (no credentials from CLI)   │
│                                                              │
│  2. LLM Request Handler (/v1/chat/completions)              │
│     ├─ Read X-Nexus-Skill header (active product)          │
│     ├─ Load SKILLS.md for that product                      │
│     ├─ Search Confluence for relevant chunks                │
│     ├─ Append RAG context to messages[1]                    │
│     ├─ Forward augmented messages to LLM via Lumin8        │
│     └─ Stream response back as SSE                          │
│                                                              │
│  3. Skill Management (/api/skills, /api/skills/{name})     │
│     ├─ Product metadata + keywords (for auto-detection)     │
│     └─ Full SKILLS.md content (for context injection)       │
│                                                              │
│  4. Error Analysis (/api/overflow/ingest)                   │
│     ├─ Receive error description + git diff + file list    │
│     ├─ Store or immediately analyze                         │
│     └─ Return optional debugging suggestion                 │
│                                                              │
└────────────────────────────────────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
    ┌────────┐      ┌────────┐      ┌────────┐
    │ the internal auth proxy  │      │ Lumin8 │      │ Conf-  │
    │ Proxy  │      │ (LLM   │      │ luence │
    │        │      │ Router)│      │ (Docs) │
    └────────┘      └───┬────┘      └────────┘
                        │
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
    rag_context = f"""## Nexus Product Context: {skill}

### Applicable Skills & Rules
{skills_md}

### Relevant Architecture Documentation
{confluence_chunks}

### Code Standards
{code_standards}

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

## API Endpoint Specifications

### 1. POST /v1/chat/completions

**Purpose**: OpenAI-compatible LLM endpoint with RAG injection

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

**Purpose**: Receive error/debugging submissions from `/solve` command

**Request Headers**: X-Nexus-Skill (optional): Current product context

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
  "suggestion": "This looks like a token expiry race condition. Check middleware order."
}
```

**Used By**: CLI `/solve` command for error analysis/team knowledge sharing

---

### 5. GET /v1/models

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

## Implementation Checklist

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

### Overflow/Error Analysis

- [ ] Accept error description, git diff, and file list
- [ ] Store for later analysis or return immediate suggestion
- [ ] Return 200 with status and optional suggestion

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

### the internal auth proxy Auth Failures
→ Return 401, prompt CLI to re-authenticate

### Lumin8/LLM Service Down
→ Return 503, suggest checking LLM status

### Confluence Unavailable
→ Continue without Confluence chunks, inject what you have

### Skill Not Found
→ Return 404 or default to "default" skill

### Message Malformed
→ Return 400 with error details

---

## Testing the Integration

### 1. Start the Mock Backend

```bash
cd /Users/aeres/Desktop/projects/nexus-cli
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

# Submit error
curl -X POST http://localhost:8000/api/overflow/ingest \
  -H "Content-Type: application/json" \
  -d '{"description":"Test error","git_diff":"","files_in_context":[]}'

# Health check
curl http://localhost:8000/v1/models
```

### 4. Full End-to-End

```bash
# Install nexus-cli
pip install -e /Users/aeres/Desktop/projects/nexus-cli

# Set credentials
mkdir -p ~/.nexus
echo '{"credentials":{"username":"test","password":"test"},"skill_mappings":{}}' > ~/.nexus/config

# Run CLI
nexus
```

---

## Performance & Optimization Tips

1. **Token Caching**: Cache the internal auth proxy tokens to avoid repeated authentication
2. **Confluence Caching**: Cache Confluence search results for 1-5 minutes
3. **Skill Metadata Caching**: Load skill list on server startup, cache for session
4. **SSE Chunking**: Send smaller chunks for more responsive UI
5. **LLM Timeout**: Set reasonable timeout (e.g., 5 min) for long responses
6. **Connection Pooling**: Reuse HTTP connections to the internal auth proxy, Lumin8, Confluence

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

1. **Lumin8 Integration**: How does Lumin8 provide streaming? (async generator? WebSocket?)
2. **Confluence**: Is Confluence available? How do we authenticate?
3. **Auth proxy**: Exact authentication flow? (Basic auth? Token exchange?)
4. **Skills Storage**: Database? Files? Config?
5. **Error Logging**: Where should errors be logged? (file? external service?)

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
✅ API endpoint specifications
✅ Implementation checklist
✅ Error handling guidance
✅ Testing instructions
✅ Performance tips
✅ Common pitfalls to avoid

**Start with**: Authentication → Health check → Chat completions → Skills → Overflow

**Test with**: Mock backend → CLI with real backend

**Deploy to**: Production the internal auth proxy + Lumin8 environment

Good luck! 🚀
