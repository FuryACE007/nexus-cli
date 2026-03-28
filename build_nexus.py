"""
Nexus CLI PyInstaller Build Script

Bundles the entire Nexus CLI (modified aider) into a single standalone executable.
Handles tree-sitter native libraries, litellm data files, and tiktoken encodings.

Usage:
    python build_nexus.py

Output:
    dist/nexus  (single-file executable)
"""

import subprocess
import sys


def build():
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "nexus",
        # Tree-sitter native grammar libraries
        "--collect-data",
        "tree_sitter_languages",
        "--collect-binaries",
        "tree_sitter_languages",
        # LiteLLM model configs and provider mappings
        "--collect-data",
        "litellm",
        # Tiktoken encoding files
        "--collect-data",
        "tiktoken_ext",
        # Aider resource files (model settings, prompts)
        "--collect-data",
        "aider",
        # Hidden imports for dynamically loaded modules
        "--hidden-import",
        "tree_sitter_languages",
        "--hidden-import",
        "tiktoken_ext.openai_public",
        "--hidden-import",
        "tiktoken_ext",
        "--hidden-import",
        "litellm.llms.openai",
        "--hidden-import",
        "litellm.llms.custom_httpx",
        "--hidden-import",
        "litellm.llms.openai_like.chat.transformation",
        "--hidden-import",
        "requests",
        "--hidden-import",
        "certifi",
        "--hidden-import",
        "charset_normalizer",
        # Entry point
        "aider/main.py",
    ]

    print("Building Nexus CLI standalone executable...")
    print(f"Command: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=sys.path[0] or ".")
    if result.returncode == 0:
        print()
        print("Build successful! Executable: dist/nexus")
        print("Copy dist/nexus to a directory in your PATH to use it globally.")
    else:
        print()
        print(f"Build failed with exit code {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    build()
