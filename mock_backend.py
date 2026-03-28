"""
Mock Nexus Backend for Integration Testing

Runs on localhost:8000 and echoes requests, allowing verification that:
1. CLI sends correct model name and API base
2. CLI sends correct skill header (X-Nexus-Skill)
3. CLI preserves SEARCH/REPLACE formatting in system prompt
4. Streaming responses work correctly
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import json
import asyncio
import uuid

app = FastAPI(title="Mock Nexus Backend", version="1.0.0")

# Store last request for verification
last_request = {}

# In-memory store for AgentOverflow issues (mock — real backend uses a DB)
# Maps issue_id → {"description": ..., "resolution": None | str, "skill": str}
_overflow_issues: dict = {}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Mock chat completions endpoint"""
    global last_request

    body = await request.json()
    headers = dict(request.headers)

    # Store request for verification
    last_request = {
        "endpoint": "/v1/chat/completions",
        "method": "POST",
        "headers": headers,
        "body": body,
        "timestamp": "now",
    }

    print(f"\n📨 Received /v1/chat/completions request")
    print(f"   Model: {body.get('model')}")
    print(f"   Skill header (X-Nexus-Skill): {headers.get('x-nexus-skill', 'not set')}")
    print(f"   Messages: {len(body.get('messages', []))} items")

    # Verify SEARCH/REPLACE system prompt is intact
    if body.get("messages"):
        first_msg = body["messages"][0]
        if first_msg.get("role") == "system":
            content = first_msg.get("content", "")
            if "SEARCH" in content and "REPLACE" in content:
                print("   ✓ SEARCH/REPLACE prompt is intact")
            else:
                print("   ✗ SEARCH/REPLACE prompt missing!")

    async def generate():
        """Stream a mock response — plan text for architect agent, SEARCH/REPLACE for code agent"""
        model_name = body.get("model", "")
        if "nexus-architect" in model_name:
            # Architect agent returns a natural language plan — NO SEARCH/REPLACE blocks
            response_text = """Here's the plan for this change:

1. Locate the target function and identify its current signature
2. Add input validation for None and negative values at the top of the function
3. Raise a ValueError with a descriptive message for invalid inputs
4. Keep all existing logic intact below the new validation block
5. Update the docstring to document the new validation behaviour
"""
        else:
            # Code agent returns SEARCH/REPLACE blocks for actual file edits
            response_text = """Here's the fix:

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
"""
        # Simulate streaming response
        for i, chunk in enumerate(response_text.split()):
            chunk_json = json.dumps(
                {
                    "id": "chatcmpl-test123",
                    "object": "chat.completion.chunk",
                    "created": 1234567890,
                    "model": "nexus-agent",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk + " "},
                            "finish_reason": None,
                        }
                    ],
                }
            )
            yield f"data: {chunk_json}\n\n"
            await asyncio.sleep(0.01)  # Simulate streaming delay

        # Final message with stop
        final_json = json.dumps(
            {
                "id": "chatcmpl-test123",
                "object": "chat.completion.chunk",
                "created": 1234567890,
                "model": "nexus-agent",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield f"data: {final_json}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/skills")
async def list_skills(request: Request):
    """List available product skills"""
    headers = dict(request.headers)
    print(f"\n📨 Received GET /api/skills")
    print(f"   Skill: {headers.get('x-nexus-skill', 'not set')}")

    return [
        {
            "name": "staking",
            "description": "Staking product - validator management",
            "keywords": ["stake", "validator", "delegation"],
        },
        {
            "name": "payments",
            "description": "Payments processing engine",
            "keywords": ["payment", "settlement", "transaction"],
        },
    ]


@app.get("/api/skills/{name}")
async def get_skill(name: str, request: Request):
    """Get a specific skill"""
    headers = dict(request.headers)
    print(f"\n📨 Received GET /api/skills/{name}")
    print(f"   Skill: {headers.get('x-nexus-skill', 'not set')}")

    skills = {
        "staking": {
            "name": "staking",
            "description": "Staking product",
            "skill_content": """# Staking Product Skills

## Architecture Rules
1. All validators must be registered before delegation
2. Stake amounts must be >= 100 SOL
3. Epoch transitions happen every 432 blocks

## Code Standards
- Use snake_case for variable names
- All state updates must emit events
- Never use mutable statics
""",
        },
        "payments": {
            "name": "payments",
            "description": "Payments processing",
            "skill_content": """# Payments Product Skills

## Settlement Rules
1. All transactions must be atomic
2. Settlement occurs T+1 after execution
3. Must support multiple currencies

## Code Standards
- Use strict type checking
- All amounts stored as integers (cents)
- Implement circuit breaker patterns
""",
        },
    }

    if name in skills:
        return skills[name]
    else:
        raise HTTPException(status_code=404, detail=f"Skill {name} not found")


@app.post("/api/overflow/ingest")
async def overflow_ingest(request: Request):
    """Receive error submissions"""
    global last_request
    body = await request.json()
    headers = dict(request.headers)

    last_request = {
        "endpoint": "/api/overflow/ingest",
        "method": "POST",
        "headers": headers,
        "body": body,
    }

    print(f"\n📨 Received POST /api/overflow/ingest")
    print(f"   Description: {body.get('description', '')[:60]}")
    print(f"   Git diff lines: {len(body.get('git_diff', '').splitlines())}")
    print(f"   Files in context: {len(body.get('files_in_context', []))}")
    print(f"   Skill: {headers.get('x-nexus-skill', 'not set')}")

    # Assign a stable issue_id for this submission
    issue_id = str(uuid.uuid4())
    skill = headers.get("x-nexus-skill", "unknown")

    # Store in mock knowledge base
    _overflow_issues[issue_id] = {
        "description": body.get("description", ""),
        "git_diff": body.get("git_diff", ""),
        "files_in_context": body.get("files_in_context", []),
        "skill": skill,
        "resolution": None,
    }

    print(f"   Assigned issue_id: {issue_id}")

    # Mock: check if any resolved issues look similar (real backend uses vector search)
    suggestion = ""
    for past_id, past in _overflow_issues.items():
        if past_id != issue_id and past["resolution"] and past["skill"] == skill:
            suggestion = f"Similar past issue resolved with: {past['resolution']}"
            break

    if not suggestion:
        suggestion = "This looks like an authentication timeout. Check your token refresh logic."

    return {
        "status": "ingested",
        "issue_id": issue_id,
        "suggestion": suggestion,
    }


@app.post("/api/overflow/resolve")
async def overflow_resolve(request: Request):
    """Record the confirmed resolution for a previously submitted AgentOverflow issue."""
    global last_request
    body = await request.json()
    headers = dict(request.headers)

    last_request = {
        "endpoint": "/api/overflow/resolve",
        "method": "POST",
        "headers": headers,
        "body": body,
    }

    issue_id = body.get("issue_id", "")
    resolution = body.get("resolution", "")

    print(f"\n📨 Received POST /api/overflow/resolve")
    print(f"   issue_id: {issue_id}")
    print(f"   Resolution: {resolution[:80]}")

    if not issue_id or issue_id not in _overflow_issues:
        raise HTTPException(
            status_code=404,
            detail=f"Issue {issue_id!r} not found. It may have already been resolved or expired.",
        )

    if not resolution:
        raise HTTPException(status_code=422, detail="resolution field is required and cannot be empty.")

    # Store the confirmed resolution
    _overflow_issues[issue_id]["resolution"] = resolution
    print(f"   ✅ Resolution stored — knowledge base now has {sum(1 for v in _overflow_issues.values() if v['resolution'])} resolved issue(s)")

    return {
        "status": "resolved",
        "message": "Resolution recorded. This fix will be surfaced for similar future issues.",
    }


@app.get("/v1/models")
async def list_models():
    """Health check endpoint"""
    print(f"\n📨 Received GET /v1/models (health check)")
    return {"data": [{"id": "nexus-agent", "object": "model"}]}


@app.get("/test/last-request")
async def get_last_request():
    """Debugging endpoint to see what the CLI sent"""
    return last_request


@app.get("/test/health")
async def health():
    """Simple health check"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    print("🚀 Starting Mock Nexus Backend on http://localhost:8000")
    print("📝 API endpoints:")
    print("   POST /v1/chat/completions   - LLM completions (streaming)")
    print("   GET  /v1/models             - Health check / model list")
    print("   GET  /api/skills            - List product skill contexts")
    print("   GET  /api/skills/{name}     - Get a skill's content")
    print("   POST /api/overflow/ingest   - Submit an error for analysis")
    print("   POST /api/overflow/resolve  - Record confirmed resolution")
    print("📝 Debug endpoints:")
    print("   GET  /test/health           - Health check")
    print("   GET  /test/last-request     - See last request sent by CLI")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000)
