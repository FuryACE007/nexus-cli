"""
Nexus CLI skill detection module.

Auto-detects which product skill context to use for the current repository
and caches the selection in ~/.nexus/config.

Authentication is handled server-side by the backend service account.
No credentials are required from the CLI.
"""

import json
import subprocess
from pathlib import Path

NEXUS_CONFIG_DIR = Path.home() / ".nexus"
NEXUS_CONFIG_FILE = NEXUS_CONFIG_DIR / "config"
NEXUS_BASE_URL = "http://localhost:8000"


def _load_config():
    """Load the nexus config file, returning a dict."""
    if NEXUS_CONFIG_FILE.exists():
        try:
            return json.loads(NEXUS_CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_config(config):
    """Save the nexus config file."""
    NEXUS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    NEXUS_CONFIG_FILE.write_text(json.dumps(config, indent=2))
    try:
        NEXUS_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def get_auth_headers(io=None):
    """
    Return HTTP headers for Nexus backend requests.

    Authentication is handled by the backend service account — no credentials
    are required from the CLI. Returns an empty dict; the X-Nexus-Skill header
    is added separately after skill detection.
    """
    return {}


def _get_repo_metadata(repo_root):
    """
    Gather metadata about the current repo to help match a skill.

    Returns a dict with directory name, git remote URL, and top-level filenames.
    """
    metadata = {
        "directory_name": "",
        "git_remote_url": "",
        "top_level_files": [],
    }

    if not repo_root:
        return metadata

    repo_path = Path(repo_root)
    metadata["directory_name"] = repo_path.name

    # Get git remote URL
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            metadata["git_remote_url"] = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Get top-level filenames
    try:
        entries = [e.name for e in repo_path.iterdir() if not e.name.startswith(".")]
        metadata["top_level_files"] = entries[:50]  # Cap to avoid huge lists
    except OSError:
        pass

    return metadata


def _score_skill(skill, metadata):
    """
    Score how well a skill matches the current repo metadata.

    Returns an integer score (higher = better match).
    """
    score = 0
    keywords = [k.lower() for k in skill.get("keywords", [])]
    skill_name = skill.get("name", "").lower()

    dir_name = metadata.get("directory_name", "").lower()
    remote_url = metadata.get("git_remote_url", "").lower()
    top_files = [f.lower() for f in metadata.get("top_level_files", [])]

    # Check directory name against skill name and keywords
    if skill_name in dir_name or dir_name in skill_name:
        score += 10

    for kw in keywords:
        if kw in dir_name:
            score += 5
        if kw in remote_url:
            score += 5
        for f in top_files:
            if kw in f:
                score += 2

    return score


def detect_active_skill(io, repo_root):
    """
    Auto-detect which product skill/context applies to the current repository.

    1. Check ~/.nexus/config for a cached skill mapping for this repo path.
    2. If no cache: GET /api/skills to list all available skills.
    3. Score each skill against repo metadata.
    4. If confident match: auto-select and cache.
    5. If ambiguous: present choices to user, cache selection.

    Returns the skill name string (e.g., "staking").
    """
    import requests

    config = _load_config()
    skill_mappings = config.get("skill_mappings", {})

    # Normalize repo path for cache key
    repo_key = str(Path(repo_root).resolve()) if repo_root else "default"

    # Check cache first
    if repo_key in skill_mappings:
        cached_skill = skill_mappings[repo_key]
        io.tool_output(f"Using cached product context: {cached_skill}")
        return cached_skill

    # Fetch available skills from backend (no auth headers needed)
    try:
        resp = requests.get(
            f"{NEXUS_BASE_URL}/api/skills",
            timeout=10,
        )
        resp.raise_for_status()
        skills = resp.json()
    except Exception as e:
        io.tool_warning(f"Could not fetch skills from backend: {e}")
        io.tool_warning("Continuing without product context.")
        return "default"

    if not skills:
        io.tool_warning("No skills available on the backend.")
        return "default"

    # Score skills against repo metadata
    metadata = _get_repo_metadata(repo_root)
    scored = [(skill, _score_skill(skill, metadata)) for skill in skills]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_skill, best_score = scored[0]

    # Confident match threshold
    if best_score >= 10 and (len(scored) < 2 or best_score > scored[1][1] * 2):
        selected = best_skill["name"]
        io.tool_output(f"Auto-detected product context: {selected}")
    else:
        # Present choices to user
        io.tool_output("Multiple product contexts available. Please select one:")
        for i, (skill, score) in enumerate(scored):
            desc = skill.get("description", "")
            io.tool_output(f"  [{i + 1}] {skill['name']}: {desc}")

        choice = io.prompt_ask(
            f"Select product context [1-{len(scored)}]:",
            default="1",
        )
        try:
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(scored):
                selected = scored[idx][0]["name"]
            else:
                selected = scored[0][0]["name"]
        except (ValueError, IndexError):
            selected = scored[0][0]["name"]

    # Cache the selection
    skill_mappings[repo_key] = selected
    config["skill_mappings"] = skill_mappings
    _save_config(config)

    return selected


def update_skill_mapping(repo_root, skill_name):
    """Update the cached skill mapping for a repo (used by @skill override)."""
    config = _load_config()
    skill_mappings = config.get("skill_mappings", {})
    repo_key = str(Path(repo_root).resolve()) if repo_root else "default"
    skill_mappings[repo_key] = skill_name
    config["skill_mappings"] = skill_mappings
    _save_config(config)
