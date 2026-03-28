"""
nexus_patches.py — Catalogue of every change nexus made to aider source files.

This is the authoritative reference for what makes nexus-cli different from
upstream aider-chat. When sync_upstream.py detects that a merge overwrote a
nexus change, you use this file to understand exactly what to restore.

Each entry is a dict with:
  file        — relative path from repo root
  description — human-readable summary of what was changed
  search      — unique string that MUST exist in the file (from signature check)
  context     — surrounding code excerpt so you know WHERE to insert it

Run this file directly to print a guide:
  python scripts/nexus_patches.py
"""

PATCHES = [
    # ─────────────────────────────────────────────────────────────────────────
    # aider/main.py — Patch 1: Hardwire LLM endpoint
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/main.py",
        "id": "main-hardwire-endpoint",
        "description": "Hardwire the nexus backend URL, model name, and API key",
        "search": "nexus-passthrough",
        "what_was_added": """
    # ── Nexus hardwiring ──────────────────────────────────────────────────
    os.environ["OPENAI_API_KEY"] = "nexus-passthrough"
    os.environ["OPENAI_API_BASE"] = "http://localhost:8000/v1"
    kwargs["model"] = "openai/nexus-agent"
    kwargs["analytics"] = False
    # ─────────────────────────────────────────────────────────────────────
""",
        "insertion_hint": "Near top of main() function, before the model is used",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # aider/main.py — Patch 2: Skill detection + health check
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/main.py",
        "id": "main-skill-detection",
        "description": "Auto-detect product skill and inject X-Nexus-Skill header; run health check",
        "search": "detect_active_skill",
        "what_was_added": """
    # ── Nexus: skill detection ────────────────────────────────────────────
    from aider.nexus_auth import detect_active_skill, get_auth_headers

    io_obj = InputOutput(...)  # already created at this point
    repo_root = get_git_root()

    # Health check — verify backend is reachable before starting
    import requests
    try:
        resp = requests.get(f"{NEXUS_BASE_URL}/v1/models", timeout=5)
        resp.raise_for_status()
    except Exception as e:
        io_obj.tool_warning(f"Nexus backend not reachable at {NEXUS_BASE_URL}: {e}")

    # Detect skill and inject header
    skill = detect_active_skill(io_obj, repo_root)
    auth_headers = get_auth_headers(io_obj)
    auth_headers["X-Nexus-Skill"] = skill

    # Attach to model so every request carries the header
    main_model._nexus_extra_headers = auth_headers
    # ─────────────────────────────────────────────────────────────────────
""",
        "insertion_hint": "After InputOutput and model are created, before the coder is started",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # aider/models.py — Patch 1: Validation bypass for nexus-agent
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/models.py",
        "id": "models-validation-bypass",
        "description": "Skip aider's API key validation for the nexus-agent model",
        "search": 'if "nexus-agent" in self.name:',
        "what_was_added": """
    def validate_environment(self):
        # ── Nexus: bypass key validation for internal model ───────────────
        if "nexus-agent" in self.name:
            return dict(keys_in_environment=True, missing_keys=[])
        # ─────────────────────────────────────────────────────────────────
        # ... original validation logic follows
""",
        "insertion_hint": "At the top of the validate_environment() method, before the original logic",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # aider/models.py — Patch 2: Header injection in send_completion
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/models.py",
        "id": "models-header-injection",
        "description": "Inject _nexus_extra_headers (X-Nexus-Skill) into every LLM request",
        "search": "_nexus_extra_headers",
        "what_was_added": """
        # ── Nexus: inject skill header ────────────────────────────────────
        if hasattr(self, "_nexus_extra_headers") and self._nexus_extra_headers:
            if "extra_headers" not in kwargs:
                kwargs["extra_headers"] = {}
            kwargs["extra_headers"].update(self._nexus_extra_headers)
        # ─────────────────────────────────────────────────────────────────
""",
        "insertion_hint": "Inside send_completion(), just before the litellm/openai call",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # aider/commands.py — Patch 1: /solve command
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/commands.py",
        "id": "commands-solve",
        "description": "Add /solve command that submits error traces to the overflow endpoint",
        "search": "def cmd_solve",
        "what_was_added": """
    def cmd_solve(self, args):
        \"\"\"/solve <description> — Submit error for analysis via Nexus backend\"\"\"
        import subprocess
        import requests
        from aider.nexus_auth import NEXUS_BASE_URL

        description = args.strip()
        if not description:
            self.io.tool_error("Usage: /solve <error description>")
            return

        # Gather git diff
        try:
            git_diff = subprocess.check_output(
                ["git", "diff", "HEAD"], text=True, timeout=10
            )
        except Exception:
            git_diff = ""

        # Files currently in context
        files_in_context = [str(f) for f in self.coder.abs_fnames]

        payload = {
            "description": description,
            "git_diff": git_diff,
            "files_in_context": files_in_context,
        }

        headers = {"Content-Type": "application/json"}
        if hasattr(self.coder.main_model, "_nexus_extra_headers"):
            headers.update(self.coder.main_model._nexus_extra_headers)

        try:
            resp = requests.post(
                f"{NEXUS_BASE_URL}/api/overflow/ingest",
                json=payload,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            suggestion = result.get("suggestion", "Submitted successfully.")
            self.io.tool_output(f"✅ Submitted. Suggestion: {suggestion}")
        except Exception as e:
            self.io.tool_error(f"Failed to submit: {e}")
""",
        "insertion_hint": "Add as a new method in the Commands class, alongside other cmd_* methods",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # aider/coders/base_coder.py — Patch 1: @skill interceptor
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "aider/coders/base_coder.py",
        "id": "base-coder-skill-interceptor",
        "description": "Intercept @skillname tags in user messages to switch product context",
        "search": "update_skill_mapping",
        "what_was_added": """
    def _handle_skill_tags(self, inp):
        \"\"\"
        Look for @skillname tags in the user message.
        If found: switch skill context and strip the tag from the message.
        \"\"\"
        import re
        from aider.nexus_auth import update_skill_mapping

        tags = re.findall(r"@(\\w+)", inp)
        if not tags:
            return inp

        # Use the last @tag if multiple are given
        skill = tags[-1]
        update_skill_mapping(self.root, skill)

        # Update the header on the model
        if hasattr(self.main_model, "_nexus_extra_headers"):
            self.main_model._nexus_extra_headers["X-Nexus-Skill"] = skill

        self.io.tool_output(f"Switched product context to: {skill}")

        # Strip all @tags + trailing whitespace, collapse multiple spaces
        cleaned = re.sub(r"@\\w+\\s*", "", inp).strip()
        cleaned = re.sub(r"\\s+", " ", cleaned).strip()
        return cleaned
""",
        "insertion_hint": "Add as a method of the Coder base class; call it at the start of run_one()",
    },

    # ─────────────────────────────────────────────────────────────────────────
    # pyproject.toml — Patch 1: rename package and entrypoint
    # ─────────────────────────────────────────────────────────────────────────
    {
        "file": "pyproject.toml",
        "id": "pyproject-rename",
        "description": "Rename package from aider-chat to nexus-cli and entrypoint from aider to nexus",
        "search": 'name = "nexus-cli"',
        "what_was_added": """
[project]
name = "nexus-cli"           # was: aider-chat

[project.scripts]
nexus = "aider.main:main"    # was: aider = "aider.main:main"
""",
        "insertion_hint": "Edit [project] section — name and [project.scripts] section",
    },
]


def print_guide():
    print("=" * 70)
    print("  nexus-cli patch catalogue")
    print("  Use this when sync_upstream.py reports missing signatures")
    print("=" * 70)

    for i, patch in enumerate(PATCHES, 1):
        print(f"\n{'─' * 70}")
        print(f"  [{i}] {patch['id']}")
        print(f"  File: {patch['file']}")
        print(f"  What: {patch['description']}")
        print(f"  Signature to verify: {repr(patch['search'])}")
        print(f"  Where to insert: {patch['insertion_hint']}")
        print(f"\n  Code to restore:")
        for line in patch["what_was_added"].splitlines():
            print(f"    {line}")

    print(f"\n{'=' * 70}")
    print("  Tip: The full working code is in aider/nexus_auth.py,")
    print("       aider/main.py, aider/models.py, aider/commands.py,")
    print("       and aider/coders/base_coder.py")
    print("=" * 70)


if __name__ == "__main__":
    print_guide()
