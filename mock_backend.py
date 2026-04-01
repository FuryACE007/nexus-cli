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
    """Always return an answer. Internally simulates the semantic cache decision.

    Decision tree (mirrors production backend):
      similarity >= 0.87  → CACHE HIT  (return stored entry, don't persist)
      0.65 <= sim < 0.87  → SIMILAR    (call LLM, don't persist)
      sim < 0.65          → NOVEL      (call LLM, persist if confidence >= 0.72)
    """
    global last_request
    body = await request.json()
    headers = dict(request.headers)

    last_request = {
        "endpoint": "/api/overflow/ingest",
        "method": "POST",
        "headers": headers,
        "body": body,
    }

    description   = body.get("description", "")
    git_diff      = body.get("git_diff", "")
    all_files     = body.get("all_files", [])
    chat_files    = body.get("chat_files", [])
    ident_mentions = body.get("ident_mentions", [])
    file_mentions  = body.get("file_mentions", [])
    lint_outcome  = body.get("lint_outcome")
    test_outcome  = body.get("test_outcome")
    dirty_files   = body.get("dirty_files", [])
    recent_commits = body.get("recent_commits", [])

    skill = headers.get("x-nexus-skill", "unknown")

    print(f"\n📨 Received POST /api/overflow/ingest")
    print(f"   Description: {description[:80]}")
    print(f"   Git diff lines: {len(git_diff.splitlines())}")
    print(f"   All files: {len(all_files)}, Chat files: {len(chat_files)}")
    print(f"   Ident mentions: {ident_mentions[:5]}")
    print(f"   Dirty files: {dirty_files}")
    print(f"   Lint: {lint_outcome}, Test: {test_outcome}")
    print(f"   Skill: {skill}")

    # ── Mock semantic cache lookup ──────────────────────────────────────────────
    # In production this is a cosine similarity search against a vector store.
    # Here we use keyword overlap as a cheap approximation.
    def _mock_similarity(stored_desc: str, query_desc: str) -> float:
        s_words = set(stored_desc.lower().split())
        q_words = set(query_desc.lower().split())
        if not s_words or not q_words:
            return 0.0
        overlap = len(s_words & q_words)
        return overlap / max(len(s_words | q_words), 1)

    best_sim = 0.0
    best_match = None
    for past_id, past in _overflow_issues.items():
        # Only searchable entries have expires_at (persisted to KB or enriched via /solved)
        if "expires_at" not in past and past.get("resolution") is None:
            continue
        sim = _mock_similarity(past["description"], description)
        if sim > best_sim:
            best_sim = sim
            best_match = (past_id, past)

    # ── Threshold routing ───────────────────────────────────────────────────────
    CACHE_HIT_THRESHOLD  = 0.87   # serve cached, skip LLM
    SIMILAR_THRESHOLD    = 0.65   # serve LLM answer, skip persistence
    CONFIDENCE_THRESHOLD = 0.72   # minimum confidence to persist novel solutions
    DEDUP_THRESHOLD      = 0.80   # dedup gate before storing

    issue_id = str(uuid.uuid4())
    cached   = False
    persisted = False
    confidence_score = None
    cache_hit_similarity = None

    if best_sim >= CACHE_HIT_THRESHOLD and best_match:
        # ── CACHE HIT ──────────────────────────────────────────────────────────
        cached = True
        cache_hit_similarity = round(best_sim, 3)
        past_id, past = best_match
        suggestion = past.get("suggestion", "")
        past["hit_count"] = past.get("hit_count", 0) + 1
        # Reset TTL on each hit (6-month rolling window)
        from datetime import datetime, timedelta
        past["expires_at"] = (datetime.utcnow() + timedelta(days=180)).isoformat()
        print(f"   ⚡ Cache hit (similarity={best_sim:.2f}) → issue {past_id}, TTL reset")
    else:
        # ── LLM PATH (mock) ────────────────────────────────────────────────────
        # Build a grounded mock suggestion using the context the CLI sent.
        context_clues = []
        if ident_mentions:
            context_clues.append(f"identifiers: {', '.join(ident_mentions[:3])}")
        if chat_files:
            context_clues.append(f"active files: {', '.join(chat_files[:3])}")
        elif file_mentions:
            context_clues.append(f"mentioned files: {', '.join(file_mentions[:3])}")
        if dirty_files:
            context_clues.append(f"dirty: {', '.join(dirty_files[:2])}")
        if recent_commits:
            context_clues.append(f"last commit: {recent_commits[0]}")

        context_str = " | ".join(context_clues)
        desc_preview = description[:60] + ("..." if len(description) > 60 else "")
        base_suggestion = f'Based on your query ("{desc_preview}")'
        if context_str:
            base_suggestion += f" and context ({context_str})"
        base_suggestion += (
            ": check your token expiry logic and ensure the refresh call fires "
            "before the expiry window, not after. Verify the middleware order in your "
            "request pipeline."
        )

        # ── Compute mock confidence score ──────────────────────────────────────
        score = 0.50
        if len(base_suggestion.split()) > 80: score += 0.10
        if len(all_files) > 5:               score += 0.05
        if lint_outcome is True:             score += 0.05
        if test_outcome is True:             score += 0.10
        if test_outcome is False:            score -= 0.10
        confidence_score = round(min(score, 1.0), 3)

        suggestion = base_suggestion
        novel = best_sim < SIMILAR_THRESHOLD
        dedup_clear = best_sim < DEDUP_THRESHOLD

        # ── Storage decision — fully automatic, no human in the loop ──────────
        # Novel query + confident response + passes dedup gate → auto-persist
        # with a flat 6-month TTL. No staging, no promotion steps.
        if novel and dedup_clear and confidence_score >= CONFIDENCE_THRESHOLD:
            persisted = True
            from datetime import datetime, timedelta
            expires_at = (datetime.utcnow() + timedelta(days=180)).isoformat()
            _overflow_issues[issue_id] = {
                "description": description,
                "git_diff": git_diff,
                "all_files": all_files,
                "chat_files": chat_files,
                "ident_mentions": ident_mentions,
                "skill": skill,
                "suggestion": suggestion,
                "confidence": confidence_score,
                "hit_count": 0,
                "resolution": None,
                "expires_at": expires_at,   # 6-month flat TTL, reset on each cache hit
            }
            print(f"   📚 Auto-persisted to KB (confidence={confidence_score:.2f}, sim={best_sim:.2f}, ttl=6mo)")
        else:
            reason = "similar_exists" if not novel else "dedup_blocked" if not dedup_clear else "low_confidence"
            print(f"   ⏭️  Served but not persisted ({reason}, sim={best_sim:.2f}, conf={confidence_score:.2f})")
            # Store a lightweight session entry so /solved can enrich if called.
            # Not returned as a cache hit (no expires_at = not searchable).
            _overflow_issues[issue_id] = {
                "description": description,
                "skill": skill,
                "suggestion": suggestion,
                "confidence": confidence_score,
                "resolution": None,
            }

    print(f"   Returning issue_id={issue_id}, cached={cached}, persisted={persisted}")

    return {
        "status": "ok",
        "issue_id": issue_id,
        "suggestion": suggestion if not cached else best_match[1].get("suggestion", ""),
        "cached": cached,
        "cache_hit_similarity": cache_hit_similarity,
        "confidence_score": confidence_score,
        "persisted": persisted,
    }


def _mock_summarize_diff(description: str, diff: str) -> str:
    """
    Simulate what the real backend does: summarize a committed diff into a
    human-readable resolution using the LLM.

    In production this calls the LLM (via Lumin8) with a prompt like:
        "An engineer was debugging: {description}
         They committed the following changes: {diff}
         In 1-2 sentences, explain what the fix was."

    Here we just extract the changed files and line counts as a mock summary.
    """
    lines = diff.splitlines()
    changed_files = [l[6:] for l in lines if l.startswith("+++ b/")]
    added   = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))

    if changed_files:
        files_str = ", ".join(changed_files[:3])
        if len(changed_files) > 3:
            files_str += f" (+{len(changed_files)-3} more)"
        return (
            f"[Auto-summarized from diff] Changes to {files_str}: "
            f"+{added} lines / -{removed} lines. "
            f"Original issue: {description[:100]}"
        )
    return f"[Auto-summarized from diff] {added} lines added, {removed} removed. Original issue: {description[:100]}"


@app.post("/api/overflow/resolve")
async def overflow_resolve(request: Request):
    """
    Record the confirmed resolution for a previously submitted AgentOverflow issue.

    Accepts either:
    - committed_diff: git show HEAD output — backend summarizes via LLM (automatic path)
    - resolution: explicit developer note (manual /solved <text> path)
    If both are present, resolution takes precedence.
    """
    global last_request
    body = await request.json()
    headers = dict(request.headers)

    last_request = {
        "endpoint": "/api/overflow/resolve",
        "method": "POST",
        "headers": headers,
        "body": body,
    }

    issue_id      = body.get("issue_id", "")
    resolution    = body.get("resolution", "")      # explicit note from developer
    committed_diff = body.get("committed_diff", "")  # raw diff from auto-path

    print(f"\n📨 Received POST /api/overflow/resolve")
    print(f"   issue_id: {issue_id}")
    if resolution:
        print(f"   Resolution (explicit): {resolution[:80]}")
    elif committed_diff:
        print(f"   Committed diff received: {len(committed_diff)} chars — will summarize via LLM")

    if not issue_id or issue_id not in _overflow_issues:
        raise HTTPException(
            status_code=404,
            detail=f"Issue {issue_id!r} not found. It may have already been resolved or expired.",
        )

    if not resolution and not committed_diff:
        raise HTTPException(
            status_code=422,
            detail="At least one of committed_diff or resolution must be provided.",
        )

    # If the developer wrote an explicit note, use it directly.
    # Otherwise, summarize the committed diff via LLM (mocked here).
    if resolution:
        final_resolution = resolution
        source = "explicit note"
    else:
        issue_description = _overflow_issues[issue_id].get("description", "")
        final_resolution = _mock_summarize_diff(issue_description, committed_diff)
        source = "LLM summary of committed diff"

    _overflow_issues[issue_id]["resolution"] = final_resolution
    # Enriched entries become (or remain) searchable — set/reset the 6-month TTL.
    # This upgrades a previously served-only session entry into a KB entry.
    from datetime import datetime, timedelta
    _overflow_issues[issue_id]["expires_at"] = (datetime.utcnow() + timedelta(days=180)).isoformat()
    # Update the suggestion to reflect the real fix (better cache quality for future hits)
    _overflow_issues[issue_id]["suggestion"] = final_resolution

    resolved_count = sum(1 for v in _overflow_issues.values() if v.get("resolution"))
    print(f"   ✅ Resolution stored via {source}, entry now searchable (TTL=6mo)")
    print(f"   Knowledge base: {resolved_count} resolved issue(s)")

    return {
        "status": "resolved",
        "message": "Fix captured. The knowledge base will surface this for similar future issues.",
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
