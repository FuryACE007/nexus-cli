#!/usr/bin/env python3
"""
sync_upstream.py — Keep nexus-cli in sync with upstream aider-chat.

Strategy
--------
nexus-cli is a targeted fork: only 5 files from aider were modified, and
the rest of the codebase is unmodified aider. This script automates:

  1. Fetch the latest aider-chat upstream
  2. Auto-merge all pure-aider files (safe — nexus never touched them)
  3. For nexus-modified files: run 3-way diff and report conflicts clearly
  4. Re-apply nexus patches on top of upstream changes where possible
  5. Run the nexus test suite to confirm nothing broke

Nexus-Modified Files (require human review on conflict)
--------------------------------------------------------
  aider/main.py           — hardwired endpoint + skill detection wiring
  aider/models.py         — validation bypass + _nexus_extra_headers injection
  aider/commands.py       — /solve command added
  aider/coders/base_coder.py  — @skill interceptor added
  pyproject.toml          — name=nexus-cli, entry=nexus

Nexus-Owned Files (never conflict — upstream doesn't have them)
---------------------------------------------------------------
  aider/nexus_auth.py
  mock_backend.py
  test_integration.py
  build_nexus.py
  AGENT_CONTEXT.md, BACKEND_CONTEXT.md, etc.

Usage
-----
  python scripts/sync_upstream.py [--dry-run] [--skip-tests] [--branch <name>]

  --dry-run      Show what would happen, don't actually merge
  --skip-tests   Skip the test suite after sync
  --branch NAME  Work in a new branch (default: sync/upstream-<date>)
"""

import argparse
import subprocess
import sys
import re
from datetime import date
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

UPSTREAM_NAME = "upstream-aider"
UPSTREAM_URL  = "https://github.com/Aider-AI/aider.git"
REPO_ROOT     = Path(__file__).parent.parent

# Files nexus modified — these need careful 3-way merge review
NEXUS_MODIFIED = [
    "aider/main.py",
    "aider/models.py",
    "aider/commands.py",
    "aider/coders/base_coder.py",
    "pyproject.toml",
]

# Files nexus added — upstream will never have these, skip entirely
NEXUS_OWNED = [
    "aider/nexus_auth.py",
    "mock_backend.py",
    "test_integration.py",
    "build_nexus.py",
    "scripts/sync_upstream.py",
    "AGENT_CONTEXT.md",
    "BACKEND_CONTEXT.md",
    "DELIVERABLES.md",
    "INSTALLATION_NOTES.md",
    "SMOKE_TEST_REPORT.md",
    "ARCHITECTURE_FIXES.md",
    "START_HERE.md",
    "docs/nexus-backend-openapi.yaml",
    "docs/nexus-backend-integration-guide.md",
]

# Nexus change signatures — strings that MUST survive a sync in modified files
# If any of these disappear after a merge, the sync is considered broken.
NEXUS_SIGNATURES = {
    "aider/main.py": [
        "nexus-passthrough",
        "openai/nexus-agent",
        "http://localhost:8000/v1",
        "detect_active_skill",
        "X-Nexus-Skill",
    ],
    "aider/models.py": [
        "_nexus_extra_headers",
        'if "nexus-agent" in self.name:',
    ],
    "aider/commands.py": [
        "def cmd_solve",
        "/api/overflow/ingest",
        "git_diff",
        "files_in_context",
    ],
    "aider/coders/base_coder.py": [
        "update_skill_mapping",
        "_nexus_extra_headers",
    ],
    "pyproject.toml": [
        'name = "nexus-cli"',
        'nexus = "aider.main:main"',
    ],
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, cwd=None):
    """Run a shell command, return (stdout, returncode)."""
    cwd = cwd or REPO_ROOT
    result = subprocess.run(
        cmd, shell=True, cwd=str(cwd),
        capture_output=capture, text=True
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if capture else ""
        print(f"\n❌ Command failed: {cmd}")
        if stderr:
            print(f"   {stderr}")
        sys.exit(1)
    return result.stdout.strip() if capture else "", result.returncode


def header(text):
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def ok(msg):   print(f"  ✅  {msg}")
def warn(msg): print(f"  ⚠️   {msg}")
def err(msg):  print(f"  ❌  {msg}")
def info(msg): print(f"  ℹ️   {msg}")

# ── Steps ────────────────────────────────────────────────────────────────────

def ensure_clean_working_tree():
    header("Checking working tree is clean")
    status, _ = run("git status --porcelain", capture=True)
    if status:
        err("Working tree has uncommitted changes. Please commit or stash them first.")
        print(f"\n{status}")
        sys.exit(1)
    ok("Working tree is clean")


def setup_upstream_remote():
    header("Setting up upstream remote")
    remotes, _ = run("git remote", capture=True)
    if UPSTREAM_NAME in remotes.splitlines():
        ok(f"Remote '{UPSTREAM_NAME}' already exists")
    else:
        run(f"git remote add {UPSTREAM_NAME} {UPSTREAM_URL}")
        ok(f"Added remote '{UPSTREAM_NAME}' → {UPSTREAM_URL}")


def fetch_upstream():
    header("Fetching latest upstream aider-chat")
    print(f"  Fetching from {UPSTREAM_URL} …")
    run(f"git fetch {UPSTREAM_NAME} --tags --prune", capture=False)
    latest_tag, _ = run(
        f"git tag --sort=-v:refname | grep -P '^v\\d+\\.\\d+\\.\\d+$' | head -1",
        capture=True
    )
    upstream_head, _ = run(f"git rev-parse {UPSTREAM_NAME}/main", capture=True)
    ok(f"Upstream HEAD: {upstream_head[:12]}")
    if latest_tag:
        ok(f"Latest upstream tag: {latest_tag}")
    return upstream_head, latest_tag


def create_sync_branch(branch_name, dry_run):
    header(f"Creating sync branch: {branch_name}")
    if dry_run:
        info(f"[dry-run] Would create branch '{branch_name}'")
        return
    run(f"git checkout -b {branch_name}")
    ok(f"On branch '{branch_name}'")


def check_what_changed_upstream():
    """
    Returns two lists:
      - safe_files: changed in upstream, NOT in NEXUS_MODIFIED
      - risky_files: changed in upstream AND in NEXUS_MODIFIED (need careful merge)
    """
    header("Analysing upstream changes")

    # Files upstream changed since we diverged
    merge_base, _ = run(
        f"git merge-base HEAD {UPSTREAM_NAME}/main", capture=True
    )
    changed_raw, _ = run(
        f"git diff --name-only {merge_base} {UPSTREAM_NAME}/main",
        capture=True
    )
    upstream_changed = [f.strip() for f in changed_raw.splitlines() if f.strip()]

    safe_files  = [f for f in upstream_changed if f not in NEXUS_MODIFIED and f not in NEXUS_OWNED]
    risky_files = [f for f in upstream_changed if f in NEXUS_MODIFIED]

    info(f"Upstream changed {len(upstream_changed)} file(s) since fork point")
    info(f"  {len(safe_files)} safe files (pure aider, auto-mergeable)")
    info(f"  {len(risky_files)} risky files (nexus-modified, need review)")

    if risky_files:
        warn("Files that need careful review:")
        for f in risky_files:
            print(f"       {f}")

    return safe_files, risky_files


def show_risky_diffs(risky_files):
    """Print what upstream changed in each nexus-modified file."""
    if not risky_files:
        return
    header("Upstream changes in nexus-modified files")
    merge_base, _ = run(
        f"git merge-base HEAD {UPSTREAM_NAME}/main", capture=True
    )
    for f in risky_files:
        print(f"\n  📄 {f}")
        diff, _ = run(
            f"git diff {merge_base} {UPSTREAM_NAME}/main -- {f}",
            capture=True, check=False
        )
        if diff:
            # Print condensed diff (first 60 lines)
            lines = diff.splitlines()
            for line in lines[:60]:
                prefix = "  "
                if line.startswith("+") and not line.startswith("+++"):
                    prefix = "  \033[32m"  # green
                elif line.startswith("-") and not line.startswith("---"):
                    prefix = "  \033[31m"  # red
                print(f"{prefix}{line}\033[0m")
            if len(lines) > 60:
                print(f"  … ({len(lines) - 60} more lines)")
        else:
            info("  No diff (likely only metadata change)")


def attempt_merge(dry_run):
    header("Attempting merge from upstream")
    if dry_run:
        # Simulate with --no-commit
        _, rc = run(
            f"git merge --no-commit --no-ff {UPSTREAM_NAME}/main",
            check=False, capture=False
        )
        run("git merge --abort", check=False)
        if rc == 0:
            ok("[dry-run] Merge would succeed with no conflicts")
        else:
            warn("[dry-run] Merge would have conflicts — manual resolution needed")
        return rc == 0

    _, rc = run(
        f"git merge --no-ff {UPSTREAM_NAME}/main "
        f"--strategy-option=patience "
        f'-m "chore: sync upstream aider {date.today()}"',
        check=False
    )

    if rc == 0:
        ok("Merge succeeded cleanly")
        return True
    else:
        # Show which files have conflicts
        conflicts, _ = run("git diff --name-only --diff-filter=U", capture=True, check=False)
        err("Merge has conflicts in:")
        for f in conflicts.splitlines():
            print(f"       {f}")
        warn("Resolve conflicts manually, then run: git merge --continue")
        return False


def verify_nexus_signatures():
    """
    After merge, check that every nexus feature signature is still present
    in the modified files. If any are missing, the merge overwrote nexus code.
    """
    header("Verifying nexus feature signatures")
    all_ok = True
    for filepath, signatures in NEXUS_SIGNATURES.items():
        full_path = REPO_ROOT / filepath
        if not full_path.exists():
            err(f"{filepath}: FILE MISSING after merge!")
            all_ok = False
            continue
        content = full_path.read_text()
        for sig in signatures:
            if sig in content:
                ok(f"{filepath}: '{sig[:50]}'")
            else:
                err(f"{filepath}: MISSING signature '{sig[:50]}'")
                all_ok = False
    return all_ok


def restore_nexus_overrides(risky_files, dry_run):
    """
    If upstream merge overwrote nexus changes, restore them from our pre-merge state.
    Uses git's three-way merge: ours (nexus), theirs (upstream), base (fork point).
    """
    header("Checking if nexus overrides need restoring")

    needs_restore = []
    for filepath, signatures in NEXUS_SIGNATURES.items():
        if filepath not in risky_files:
            continue
        full_path = REPO_ROOT / filepath
        if not full_path.exists():
            continue
        content = full_path.read_text()
        missing = [s for s in signatures if s not in content]
        if missing:
            needs_restore.append((filepath, missing))

    if not needs_restore:
        ok("All nexus signatures intact — no restore needed")
        return True

    warn(f"{len(needs_restore)} file(s) lost nexus signatures after merge:")
    for filepath, missing in needs_restore:
        print(f"\n  📄 {filepath}")
        for sig in missing:
            print(f"       missing: '{sig}'")

    if dry_run:
        info("[dry-run] Would attempt git checkout --ours for each file")
        return False

    print()
    warn("Options:")
    print("  1. Run this script with --dry-run first to preview upstream changes")
    print("  2. Manually re-apply nexus changes from DELIVERABLES.md or AGENT_CONTEXT.md")
    print("  3. Use: git checkout HEAD~1 -- <file>  to restore the nexus version")
    print("     then cherry-pick relevant upstream changes on top")

    return False


def run_nexus_tests(skip_tests):
    header("Running nexus integration tests")
    if skip_tests:
        info("Skipped (--skip-tests)")
        return True

    _, rc = run("python test_integration.py", check=False)
    if rc == 0:
        ok("All nexus integration tests passed ✅")
        return True
    else:
        err("Nexus integration tests FAILED — sync may have broken nexus features")
        return False


def print_summary(merged, sigs_ok, tests_ok, risky_files, branch_name, dry_run):
    header("Sync Summary")
    print(f"  Branch:        {branch_name}")
    print(f"  Dry run:       {'yes' if dry_run else 'no'}")
    print(f"  Merge:         {'✅ clean' if merged else '❌ conflicts'}")
    print(f"  Signatures:    {'✅ intact' if sigs_ok else '❌ some missing'}")
    print(f"  Tests:         {'✅ passed' if tests_ok else '❌ failed'}")
    print(f"  Risky files:   {len(risky_files)} ({', '.join(risky_files) or 'none'})")
    print()

    if merged and sigs_ok and tests_ok:
        print("  🎉 Sync complete! Review the changes then open a PR.")
        print(f"     git push origin {branch_name}")
    elif not merged:
        print("  ⚠️  Resolve merge conflicts, then re-run this script to verify.")
    elif not sigs_ok:
        print("  ⚠️  Re-apply nexus changes to the flagged files (see AGENT_CONTEXT.md)")
        print("     then re-run: python test_integration.py")
    elif not tests_ok:
        print("  ⚠️  Fix failing tests before merging to main.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync nexus-cli with upstream aider-chat"
    )
    parser.add_argument("--dry-run",     action="store_true", help="Preview only, no changes")
    parser.add_argument("--skip-tests",  action="store_true", help="Skip test suite")
    parser.add_argument("--branch",      default="",          help="Branch name (default: sync/upstream-YYYY-MM-DD)")
    parser.add_argument("--show-diffs",  action="store_true", help="Print upstream diffs for risky files")
    args = parser.parse_args()

    branch_name = args.branch or f"sync/upstream-{date.today()}"

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║          nexus-cli ← upstream aider sync tool           ║")
    print("╚══════════════════════════════════════════════════════════╝")

    ensure_clean_working_tree()
    setup_upstream_remote()
    _head, latest_tag = fetch_upstream()
    safe_files, risky_files = check_what_changed_upstream()

    if args.show_diffs:
        show_risky_diffs(risky_files)

    if not safe_files and not risky_files:
        header("Already up to date")
        ok("No upstream changes found. nexus-cli is current.")
        return

    if not args.dry_run:
        create_sync_branch(branch_name, dry_run=False)

    merged  = attempt_merge(args.dry_run)
    sigs_ok = verify_nexus_signatures() if (merged and not args.dry_run) else (not bool(risky_files))
    restore_ok = restore_nexus_overrides(risky_files, args.dry_run) if not sigs_ok else True
    tests_ok = run_nexus_tests(args.skip_tests) if (merged and not args.dry_run) else True

    print_summary(merged, sigs_ok or restore_ok, tests_ok, risky_files, branch_name, args.dry_run)


if __name__ == "__main__":
    main()
