# Nexus CLI

> **Enterprise AI Coding Assistant** — A fork of [aider-chat](https://aider.chat) configured for zero-config deployment via Torii proxy + internal Nexus backend.

One command: `nexus`. No API keys, no config, no LLM selection.

---

## ✨ Features

- **Zero-config** — Auto-detects product context, no credentials needed
- **Smart editing** — SEARCH/REPLACE block format for precise code changes
- **Product skills** — `@staking` / `@payments` to switch context mid-session
- **Error analysis** — `/solve` to submit bugs for team triage
- **Zero-config auth** — Backend handles authentication via service account
- **Standalone binary** — Single executable for end users

---

## 🚀 Installation

### Prerequisites

- **Python 3.11+** (3.14+ verified)
- **UV** (recommended package manager)
- **Nexus backend** running at `localhost:8000`

### Option A — Development checkout (most common)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <nexus-cli-repo>
cd nexus-cli
uv sync
```

Run with:
```bash
uv run nexus
```

> `uv run` keeps everything inside the project's virtual environment. You need to be inside the `nexus-cli` directory.

### Option B — Global install

Install globally so you can run `nexus` from any directory:

```bash
cd nexus-cli
uv pip install -e .
```

The `-e` flag means "editable install" — changes to the code in `nexus-cli/` are reflected immediately without reinstalling.

Run from anywhere:
```bash
$ cd /any/other/project
$ nexus
```

**Verify it worked:**
```bash
which nexus              # Should show /path/to/.venv/bin/nexus
nexus --version         # Should show version
```

**Update later:**
```bash
cd nexus-cli
git pull
# uv picks up the changes automatically (editable install)
```

**Uninstall:**
```bash
uv pip uninstall nexus-cli
```

### Option C — Standalone binary (end users, no Python needed)

```bash
python build_nexus.py        # builds dist/nexus
sudo cp dist/nexus /usr/local/bin/
nexus
```

---

## 🖥️ First Run

```
$ uv run nexus

Detecting product context from repo...
✓ Auto-detected product context: staking
✓ Backend healthy (http://localhost:8000)

> _
```

No credentials needed. Authentication is handled server-side by the backend service account.

The detected skill is cached in `~/.nexus/config`. To reset it:
```bash
rm ~/.nexus/config
uv run nexus
```

---

## 💬 Chat Modes

Nexus CLI has four chat modes. Switch between them mid-session:

| Mode | Command | What it does |
|------|---------|-------------|
| **code** (default) | `/code` | Ask for code changes — produces edits |
| **ask** | `/ask` | Ask questions without making any changes |
| **architect** | `/architect` | High-level planning via architect agent, then auto-applies edits |
| **context** | `/context` | Explore surrounding code context |

You can also inline a mode for a single message:
```
/ask why does this function use a global lock?
/code refactor the auth middleware to use a context manager
```

---

## 📖 Product Skills (@skill)

Skills are product-specific contexts (code standards, architecture docs, Confluence content) loaded from the Nexus backend.

### Auto-detection

On first run in a repo, Nexus scores available skills against the repo name, git remote, and filenames. If confident, it auto-selects. Otherwise, it prompts you to choose.

### Switching skills mid-session

Type `@skillname` anywhere in your message:

```
> @staking fix the consensus validator
```

What happens:
1. CLI fetches the `staking` skill from the backend
2. Sets `X-Nexus-Skill: staking` on all subsequent requests
3. Strips `@staking` from the message
4. Sends: `fix the consensus validator` — backend injects staking context

You can switch at any time:
```
> @payments now update the settlement flow
```

The new skill stays active for the rest of the session.

### Listing available skills

Skills are defined in the backend. Ask your backend team for the full list, or check the output at startup:
```
✓ Auto-detected product context: staking
```

---

## 📁 File Management

Before Nexus can edit a file, it needs to be added to the chat context.

```bash
/add src/auth.py                  # add a single file
/add src/auth.py src/middleware.py # add multiple files
/add src/                         # add a whole directory
/read-only src/config.py          # add as read-only reference (no edits)
/drop src/auth.py                 # remove a file from context
/ls                               # list all files in context
/tokens                           # see how much context is being used
```

**Tip**: Keep context tight. The more files you add, the more tokens you use and the slower responses get. Add only what's relevant to your current task.

---

## ⌨️ Command Reference

All commands start with `/`. Tab-completion is available.

### File Context

| Command | Usage | Description |
|---------|-------|-------------|
| `/add` | `/add <file> [file...]` | Add files to chat for editing |
| `/read-only` | `/read-only <file> [file...]` | Add files as read-only reference |
| `/drop` | `/drop [file...]` | Remove files from chat context |
| `/ls` | `/ls` | List all files in chat and repo |
| `/tokens` | `/tokens` | Show token usage for current context |
| `/map` | `/map` | Print the current repository map |
| `/map-refresh` | `/map-refresh` | Force a refresh of the repo map |

### Chat & Modes

| Command | Usage | Description |
|---------|-------|-------------|
| `/ask` | `/ask [question]` | Ask without editing files; or switch to ask mode |
| `/code` | `/code [request]` | Request code changes; or switch to code mode |
| `/architect` | `/architect [prompt]` | Plan changes via architect agent, then apply with code agent |
| `/context` | `/context [prompt]` | Explore code context; or switch to context mode |
| `/chat-mode` | `/chat-mode <mode>` | Explicitly switch mode (`ask`, `code`, `architect`, `context`) |
| `/ok` | `/ok [addl text]` | Shortcut for "Ok, go ahead and make those changes" |
| `/clear` | `/clear` | Clear chat history (files stay in context) |
| `/reset` | `/reset` | Drop all files AND clear chat history |

### Git

| Command | Usage | Description |
|---------|-------|-------------|
| `/commit` | `/commit [message]` | Commit changes made outside the chat |
| `/undo` | `/undo` | Undo the last Nexus-made git commit |
| `/diff` | `/diff` | Show diff of changes since last message |
| `/git` | `/git <args>` | Run any git command (output not shown in chat) |

### Code Quality

| Command | Usage | Description |
|---------|-------|-------------|
| `/lint` | `/lint [file...]` | Lint and auto-fix in-context files |
| `/test` | `/test <cmd>` | Run a test command; on failure, adds output to chat |
| `/run` | `/run <cmd>` | Run any shell command; optionally add output to chat |

### Nexus-Specific

| Command | Usage | Description |
|---------|-------|-------------|
| `/solve` | `/solve <description>` | Submit an error for AgentOverflow analysis |
| `/web` | `/web <url>` | Scrape a webpage and send contents as context |
| `/paste` | `/paste [name]` | Paste image or text from clipboard into chat |
| `/voice` | `/voice` | Record and transcribe voice input |

### Session & Config

| Command | Usage | Description |
|---------|-------|-------------|
| `/settings` | `/settings` | Show all current settings |
| `/save` | `/save <file>` | Save session (files + commands) to a file |
| `/load` | `/load <file>` | Load and replay a saved session file |
| `/copy` | `/copy` | Copy the last assistant message to clipboard |
| `/copy-context` | `/copy-context` | Copy full chat context as markdown |
| `/editor` | `/editor` | Open `$EDITOR` to write a long prompt |
| `/multiline-mode` | `/multiline-mode` | Toggle multiline mode (Enter vs Meta+Enter) |
| `/help` | `/help [question]` | Ask questions about how Nexus/aider works |
| `/exit` | `/exit` | Exit Nexus CLI |

### Model Controls (Advanced)

| Command | Usage | Description |
|---------|-------|-------------|
| `/model` | `/model <name>` | Switch the main model (normally locked to `nexus-agent`) |
| `/models` | `/models [search]` | Search available models |
| `/reasoning-effort` | `/reasoning-effort <low\|medium\|high>` | Set reasoning effort level |
| `/think-tokens` | `/think-tokens <n>` | Set thinking token budget (e.g. `8k`, `0.5M`, `0` to disable) |

---

## 🛠️ /solve — Error Analysis

`/solve` captures your current state and submits it to the Nexus AgentOverflow API for triage.

```
> /solve the token refresh is returning 403 with valid credentials
```

What gets sent to the backend:
- Your description
- Current `git diff` (uncommitted changes)
- Files currently in chat context

Example:
```
> /add src/auth.py
> /solve the refresh endpoint fails after 10 minutes but works on cold start
✓ Overflow analysis submitted successfully.
Suggestion: This is likely a clock skew issue between the token issuer and validator.
            Check server NTP sync. Token TTL is 600s; if clocks differ by > 30s,
            validation fails on re-use.
```

---

## 🧪 Testing & Development

```bash
# Run integration tests
uv run python test_integration.py

# Start mock backend (logs all CLI requests)
uv run python mock_backend.py

# Build standalone binary
uv run python build_nexus.py
./dist/nexus --version
```

---

## 🔧 Documentation

| Document | For | Time |
|----------|-----|------|
| [START_HERE.md](START_HERE.md) | **New users** — navigation guide | 5 min |
| [AGENT_CONTEXT.md](AGENT_CONTEXT.md) | **LLM agents / contributors** — all code changes + backend spec | 30 min |
| [INSTALLATION_NOTES.md](INSTALLATION_NOTES.md) | **Installers** — UV, pip, binary, Docker | 10 min |
| [docs/nexus-backend-integration-guide.md](docs/nexus-backend-integration-guide.md) | **Backend devs** — API contract | 30 min |
| [BACKEND_CONTEXT.md](BACKEND_CONTEXT.md) | **Backend devs** — deep request/response flow | 60 min |
| [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) | **QA** — test results + checklist | 10 min |
| [DELIVERABLES.md](DELIVERABLES.md) | **DevOps** — file inventory + deployment | 10 min |

---

## 🆘 Troubleshooting

**`Cannot reach Nexus backend at http://localhost:8000`**
```bash
curl http://localhost:8000/v1/models   # Should return 200 with model list
```
→ Backend is not running. Start it before running `nexus`.

**`Skill detection failed`**
→ Backend `/api/skills` is unreachable. Check credentials and network. Nexus will continue without product context.

**Edits not applying to files**
→ The LLM response didn't contain SEARCH/REPLACE blocks. This usually means the backend modified `messages[0]` (the aider system prompt). See [BACKEND_CONTEXT.md](BACKEND_CONTEXT.md).

**Authentication failed / want to re-enter credentials**
```bash
rm ~/.nexus/config
uv run nexus
```

**Want verbose output to debug requests**
```bash
uv run nexus --verbose
```

---

**Built on [aider's](https://aider.chat) editing engine.**
