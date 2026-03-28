# Installation Notes

## Python Version Requirement

Nexus CLI (fork of aider-chat) requires **Python 3.11 or higher**.

The reason for 3.11+ (not 3.10) is the numpy dependency:
- `numpy<2` is required for Python <3.11
- `numpy>=2.3` is required for Python >=3.11
- To avoid dependency conflicts, we target Python 3.11+

---

## ✅ Installation Verified on Python 3.14.3

Installation has been tested and verified to work successfully.

### Installation with UV (Recommended) ✅

```bash
# Install UV (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Navigate to nexus-cli
cd /Users/aeres/Desktop/projects/nexus-cli

# Install dependencies and create virtual environment
uv sync

# Run nexus
uv run nexus --version
# Output: nexus 0.86.3.dev34+gbdb4d9ff8.d20260328
```

**Status**: ✅ Successfully verified on Python 3.14.3

### Installation with Standard Pip

```bash
# Ensure Python 3.11+ is active
python3.11 -m pip install -e /Users/aeres/Desktop/projects/nexus-cli

# Run nexus
nexus --version
```

### Installation with Docker

```bash
docker run -it python:3.14 bash
pip install -e /path/to/nexus-cli
nexus
```

---

## First Run

Once installed, run the CLI:

```bash
$ uv run nexus

Nexus CLI will detect the product context for your repository.




📨 Detecting product context from repo...
✓ Auto-detected product context: staking
✓ Backend healthy (http://localhost:8000)

[Chat interface opens...]
```

---

## For Development/Testing

If you're developing on Nexus CLI:

```bash
# Clone/navigate to repo
cd /Users/aeres/Desktop/projects/nexus-cli

# Set up development environment
uv sync

# Run with auto-reload (during development)
uv run nexus

# Run tests
uv run python test_integration.py

# Run mock backend for testing
uv run python mock_backend.py
```
