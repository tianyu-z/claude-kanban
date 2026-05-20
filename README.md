# Code Agent Kanban

A real-time web dashboard that monitors AI coding sessions across multiple servers (currently supports [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex), displaying them as a kanban board with AI-powered task summaries.

If you run coding agents on multiple machines simultaneously, it's easy to lose track of what each agent is doing. This tool SSHs into your servers, collects sessions, and presents them in a single unified view.

![Claude Code Agent Kanban Screenshot](screenshot.png)

## Fork changes (vs. [ShenJiahuan/claude-kanban](https://github.com/ShenJiahuan/claude-kanban))

This fork diverges from upstream in three ways:

### 1. Opencode support + multi-provider checkboxes

Upstream supports only `claude` and `codex` as a single dropdown choice. This fork:
- Adds **Opencode** as a third provider — reads sessions directly from the Opencode v2 SQLite database at `~/.local/share/opencode/opencode.db`
- Replaces the provider dropdown with **checkboxes** so you can scan any subset (Claude only / Claude + Opencode / all three / etc.)
- Each session card shows the **model name** when available (e.g. `DeepSeek-V4-Pro`, `Kimi-K2.6`), useful for Opencode where you switch between providers per session

Config schema changed from `provider: <string>` to `providers: [<list>]` — old configs (including `provider: both` from the previous fork version) migrate automatically.

### 2. Remote collection shells out to system `ssh` (instead of paramiko)

Upstream uses [paramiko](https://www.paramiko.org/) for remote SSH. paramiko does **not** honor `ControlMaster`, `IdentityAgent`, or `ProxyCommand` from your `~/.ssh/config`. For users with hardware-backed agents (Secretive, YubiKey, Krypton), that means a fingerprint / touch prompt on *every* dashboard refresh — unusable.

This fork replaces the paramiko call with a `subprocess.run(["ssh", host, ...])`, so:
- Your full `~/.ssh/config` is honored (including `IdentityAgent`, `ProxyCommand`, `ControlMaster`)
- With `ControlMaster auto` + `ControlPersist yes`, you confirm once per host then the multiplex socket handles all later polls — same behavior as VSCode Remote-SSH
- The `host` field in the server config can be a plain SSH alias (e.g. `gpu-prod-0`) — ssh resolves hostname/user/port from your config

Set up `ControlMaster` in `~/.ssh/config` if you haven't already:

```
Host *
  ControlMaster auto
  ControlPath ~/.ssh/sockets/%C
  ControlPersist yes
```

### Install this fork

```bash
git clone https://github.com/tianyu-z/claude-kanban.git
cd claude-kanban
uv run claude-kanban
```

## Features

- **Multi-server monitoring** — SSH into remote servers to collect session data in parallel
- **Live kanban board** — Sessions organized into Running / Completed / Errors columns
- **Multi-provider support** — any subset of `claude`, `codex`, `opencode` via checkboxes (this fork)
- **AI-powered summaries** — Sessions are summarized by local CLI (`claude` or `codex`) with task description, progress status, and estimated completion percentage
- **Auto-refresh** — Dashboard updates automatically; fast-polls on first load to pick up AI summaries quickly
- **Session lifecycle tracking** — Sessions move from Running to Completed when the Claude Code process exits; resumed sessions move back to Running
- **Web-based configuration** — Add/edit/remove SSH servers and test connections from the Settings panel, no config files needed
- **Summary caching** — Summaries are cached on disk; unchanged sessions won't be re-summarized, even across restarts

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- For `claude` provider: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- For `codex` provider: Codex CLI installed (`codex`)
- SSH key-based access to remote servers (for multi-server monitoring)

## Quick Start

### Install with uv (recommended)

```bash
uvx claude-kanban
```

### Install with pip

```bash
pip install claude-kanban
claude-kanban
```

### Run from source

```bash
git clone https://github.com/ShenJiahuan/claude-kanban.git
cd claude-kanban
uv run claude-kanban
```

Open http://localhost:5555 in your browser.

By default, it scans the local machine for Claude Code sessions. You can switch provider to Codex in **Settings**.

## How It Works

1. **Session discovery** — Scans provider-specific session logs (`~/.claude/...` or `~/.codex/...`) on each server
2. **Conversation parsing** — Extracts first task messages and latest progress messages from JSONL logs
3. **AI summarization** — Sends conversation excerpts (first 3 + last 6 messages) to local CLI (`claude -p` or `codex exec`)
4. **Lifecycle tracking** — Sessions move between Running and Completed based on activity/process state

## Configuration

All configuration is done through the web UI (**Settings** button). Under the hood, settings are persisted to `~/.claude-kanban/config.yaml`:

```yaml
include_local: true
providers:        # any non-empty subset of: claude, codex, opencode
  - claude
  - opencode
servers:
  - host: gpu-server-1.example.com
    user: ubuntu
    label: GPU Server 1
  - host: 10.0.0.50
    user: root
    port: 2222
    key: ~/.ssh/id_ed25519
    label: Dev Box
```

### Server options

| Field   | Required | Default          | Description                    |
|---------|----------|------------------|--------------------------------|
| `host`  | Yes      | —                | Hostname or IP                 |
| `user`  | No       | Current user     | SSH username                   |
| `port`  | No       | 22               | SSH port                       |
| `key`   | No       | `~/.ssh/id_rsa`  | Path to SSH private key        |
| `label` | No       | Same as host     | Display name on the dashboard  |

### Remote server requirements

- Python 3 installed
- For `claude` provider: Claude Code installed (sessions stored in `~/.claude/`)
- For `codex` provider: Codex installed (sessions stored in `~/.codex/`)
- For `opencode` provider: Opencode v2 installed (SQLite DB at `~/.local/share/opencode/opencode.db`)
- SSH key-based authentication configured

### Environment variables

| Variable         | Default                        | Description               |
|------------------|--------------------------------|---------------------------|
| `KANBAN_CONFIG`  | `~/.claude-kanban/config.yaml` | Path to configuration file |
| `KANBAN_DATA_DIR`| `~/.claude-kanban`             | Data directory for config and summary cache |

## API

| Endpoint                         | Method | Description                          |
|----------------------------------|--------|--------------------------------------|
| `GET /api/sessions`              | GET    | Returns all sessions (cached 30s)    |
| `POST /api/refresh`              | POST   | Force refresh and return sessions    |
| `GET /api/servers`               | GET    | List configured servers              |
| `POST /api/servers`              | POST   | Add a server                         |
| `PUT /api/servers/<id>`          | PUT    | Update a server                      |
| `DELETE /api/servers/<id>`       | DELETE | Remove a server                      |
| `POST /api/servers/<id>/test`    | POST   | Test SSH connection                  |
| `PUT /api/config/local`          | PUT    | Toggle local machine scanning        |
| `PUT /api/config/providers`      | PUT    | Set active providers (array of `claude` / `codex` / `opencode`) |

## License

MIT
