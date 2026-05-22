#!/usr/bin/env python3
"""Code Agent Kanban - Web dashboard for monitoring AI coding sessions across servers."""

import glob
import json
import os
import platform
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import yaml
from flask import Flask, jsonify, request, send_from_directory

_PKG_DIR = Path(__file__).resolve().parent
_DATA_DIR = Path(os.environ.get("KANBAN_DATA_DIR", str(Path.home() / ".claude-kanban")))

app = Flask(__name__, static_folder=str(_PKG_DIR / "static"))

CONFIG_PATH = os.environ.get("KANBAN_CONFIG", str(_DATA_DIR / "config.yaml"))
CACHE_TTL = 30  # seconds
CODEX_ACTIVE_WINDOW_SEC = 300
MAX_CODEX_SESSIONS = 100
SUMMARIZER_MODEL_CLAUDE = os.environ.get("KANBAN_SUMMARY_MODEL_CLAUDE", "haiku")
SUMMARIZER_MODEL_CODEX = os.environ.get("KANBAN_SUMMARY_MODEL_CODEX", "")
PROVIDERS = {
    "claude": {
        "label": "Claude Code",
        "session_dir": ".claude/sessions",
        "project_dir": ".claude/projects",
    },
    "codex": {
        "label": "Codex",
        "session_dir": ".codex/sessions",
        "project_dir": ".codex/sessions",
    },
    "opencode": {
        "label": "Opencode",
        "session_dir": ".local/share/opencode",
        "project_dir": ".local/share/opencode",
    },
}
_cache = {"data": None, "ts": 0, "lock": threading.Lock()}

# Summary cache: {sessionId: {"messageCount": N, "summary": {...}}}
# Persisted to disk so summaries survive restarts.
SUMMARY_CACHE_PATH = _DATA_DIR / ".summary_cache.json"
_summary_cache = {"data": {}, "lock": threading.Lock()}


def _load_summary_cache():
    if SUMMARY_CACHE_PATH.exists():
        try:
            with open(SUMMARY_CACHE_PATH) as f:
                _summary_cache["data"] = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass


def _save_summary_cache():
    try:
        with open(SUMMARY_CACHE_PATH, "w") as f:
            json.dump(_summary_cache["data"], f, ensure_ascii=False)
    except OSError as e:
        print(f"[WARN] Failed to save summary cache: {e}")

# Track sessions seen as running — when they disappear from active, move to completed
_known_sessions = {"lock": threading.Lock(), "running": {}, "completed": {}}


def _default_config():
    return {"providers": ["claude"], "include_local": True, "servers": []}


_PROVIDER_ALIASES = {
    "all": list(PROVIDERS.keys()),
    "both": ["claude", "codex"],  # legacy alias from earlier fork version
}


def _normalize_providers(value):
    """Coerce config input into a clean list of valid provider keys.

    Accepts: list of strings, single string, "all"/"both" aliases.
    Preserves order, dedupes, drops unknown values.
    """
    if isinstance(value, str):
        if value in _PROVIDER_ALIASES:
            return list(_PROVIDER_ALIASES[value])
        return [value] if value in PROVIDERS else []
    if isinstance(value, list):
        out = []
        seen = set()
        for v in value:
            for p in _normalize_providers(v):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out
    return []


def _normalize_config(config):
    cfg = dict(config or {})
    # Migrate legacy `provider` single-string key to `providers` list
    providers = _normalize_providers(cfg.get("providers", cfg.get("provider")))
    cfg["providers"] = providers or ["claude"]
    cfg.pop("provider", None)
    cfg["include_local"] = bool(cfg.get("include_local", True))
    servers = cfg.get("servers")
    cfg["servers"] = servers if isinstance(servers, list) else []
    return cfg


def load_config():
    path = Path(CONFIG_PATH)
    if not path.exists():
        return _default_config()
    with open(path) as f:
        return _normalize_config(yaml.safe_load(f) or _default_config())


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(_normalize_config(config), f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Local collection
# ---------------------------------------------------------------------------

def collect_local_claude():
    """Collect currently active Claude Code sessions from the local machine.

    Only returns sessions that have an entry in ~/.claude/sessions/ (i.e. running).
    Completed sessions are tracked separately via _known_sessions.
    """
    home = Path.home()
    sessions_dir = home / ".claude" / "sessions"
    projects_dir = home / ".claude" / "projects"
    server_name = platform.node() or "localhost"

    if not sessions_dir.exists():
        return []

    sessions = []
    for sf in sessions_dir.glob("*.json"):
        try:
            meta = json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        session_id = meta.get("sessionId", "")
        if not session_id:
            continue

        # Skip subagent sessions
        if session_id.startswith("agent-"):
            continue

        pid = meta.get("pid")
        alive = _pid_alive_local(pid) if pid else False
        cwd = meta.get("cwd", "")
        started_at = meta.get("startedAt", 0)

        # Find and parse matching JSONL
        jsonl_path = _find_jsonl(session_id, projects_dir)
        task_summary = ""
        last_activity = started_at
        message_count = 0
        excerpt = []
        token_usage = {"totalInputTokens": 0, "totalOutputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0}
        if jsonl_path:
            task_summary, last_activity, message_count, excerpt, token_usage = _parse_jsonl(jsonl_path, started_at)

        # Skip summarization sessions
        if task_summary and "concise status reporter" in task_summary:
            continue

        started_dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat() if started_at else ""
        last_dt = ""
        if last_activity and last_activity != started_at:
            last_dt = datetime.fromtimestamp(last_activity / 1000, tz=timezone.utc).isoformat()

        sessions.append({
            "sessionId": session_id,
            "pid": pid,
            "cwd": cwd,
            "project": os.path.basename(cwd) if cwd else "",
            "startedAt": started_dt,
            "lastActivity": last_dt,
            "kind": meta.get("kind", ""),
            "entrypoint": meta.get("entrypoint", ""),
            "alive": alive,
            "taskSummary": task_summary,
            "messageCount": message_count,
            "tokenUsage": token_usage,
            "conversationExcerpt": excerpt,
            "server": server_name,
        })

    return sessions


def _parse_iso_timestamp_ms(ts_str):
    if not ts_str:
        return 0
    try:
        return int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _extract_codex_text_content(content):
    """Extract plain text from Codex message content items."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") in ("input_text", "output_text", "text"):
            texts.append(c.get("text", ""))
    return " ".join(t for t in texts if t).strip()


def _parse_codex_session(jsonl_path, server_name):
    """Parse a Codex session JSONL file."""
    session_id = ""
    cwd = ""
    started_at = 0
    task_summary = ""
    last_ts = 0
    message_count = 0
    first_messages = []
    recent_messages = []

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_ms = _parse_iso_timestamp_ms(entry.get("timestamp", ""))
                if ts_ms > last_ts:
                    last_ts = ts_ms

                if entry.get("type") == "session_meta":
                    payload = entry.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd", cwd)
                    started_at = _parse_iso_timestamp_ms(payload.get("timestamp", "")) or started_at
                    continue

                if entry.get("type") != "response_item":
                    continue

                payload = entry.get("payload", {})
                if payload.get("type") != "message":
                    continue

                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue

                message_count += 1
                text = _extract_codex_text_content(payload.get("content", ""))
                if not text or text.startswith("<"):
                    continue

                msg_entry = {"role": role, "text": text[:500]}
                if len(first_messages) < 3:
                    first_messages.append(msg_entry)
                recent_messages.append(msg_entry)
                if len(recent_messages) > 6:
                    recent_messages.pop(0)

                if not task_summary and role == "user":
                    task_summary = text[:300]
    except OSError:
        return None

    try:
        mtime_ms = int(jsonl_path.stat().st_mtime * 1000)
        if mtime_ms > last_ts:
            last_ts = mtime_ms
    except OSError:
        pass

    if not started_at:
        started_at = last_ts

    if task_summary and "concise status reporter" in task_summary:
        return None

    first_texts = {m["text"] for m in first_messages}
    excerpt = list(first_messages)
    for m in recent_messages:
        if m["text"] not in first_texts:
            excerpt.append(m)

    now_ms = int(time.time() * 1000)
    active_window_ms = CODEX_ACTIVE_WINDOW_SEC * 1000
    alive = bool(last_ts and (now_ms - last_ts) <= active_window_ms)

    started_dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat() if started_at else ""
    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).isoformat() if last_ts else ""

    sid = session_id or jsonl_path.stem
    return {
        "sessionId": sid,
        "pid": None,
        "cwd": cwd,
        "project": os.path.basename(cwd) if cwd else "",
        "startedAt": started_dt,
        "lastActivity": last_dt,
        "kind": "codex",
        "entrypoint": "codex",
        "alive": alive,
        "taskSummary": task_summary,
        "messageCount": message_count,
        "tokenUsage": {"totalInputTokens": 0, "totalOutputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0},
        "conversationExcerpt": excerpt,
        "server": server_name,
    }


def collect_local_codex():
    """Collect Codex sessions from ~/.codex/sessions."""
    home = Path.home()
    sessions_dir = home / ".codex" / "sessions"
    server_name = platform.node() or "localhost"
    if not sessions_dir.exists():
        return []

    session_files = sorted(
        sessions_dir.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:MAX_CODEX_SESSIONS]

    sessions = []
    for sf in session_files:
        parsed = _parse_codex_session(sf, server_name)
        if parsed:
            sessions.append(parsed)
    return sessions


def collect_local(provider):
    if provider == "codex":
        return collect_local_codex()
    if provider == "opencode":
        return collect_local_opencode()
    return collect_local_claude()


def collect_local_opencode():
    """Collect Opencode v2 sessions from the local SQLite database."""
    import sqlite3
    home = Path.home()
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    server_name = platform.node() or "localhost"
    if not db_path.exists():
        return []
    try:
        sessions = _opencode_query(str(db_path))
    except sqlite3.Error as e:
        print(f"[WARN] opencode SQLite error: {e}")
        return []
    for s in sessions:
        s["server"] = server_name
    return sessions


def _opencode_query(db_path):
    """Run the opencode session query against a SQLite database path."""
    import sqlite3
    MAX = 100
    ACTIVE_WINDOW_MS = 5 * 60 * 1000
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, title, model, agent, directory, time_created, time_updated "
        "FROM session WHERE parent_id IS NULL AND time_archived IS NULL "
        "ORDER BY time_updated DESC LIMIT ?",
        (MAX,),
    ).fetchall()
    now_ms = int(time.time() * 1000)
    sessions = []
    for sid, title, model_json, agent, cwd, t_created, t_updated in rows:
        try:
            model = json.loads(model_json) if model_json else {}
        except (json.JSONDecodeError, TypeError):
            model = {}
        model_id = model.get("id") or ""

        msg_count = cur.execute(
            "SELECT count(*) FROM message WHERE session_id=?", (sid,)
        ).fetchone()[0]

        cur.execute(
            "SELECT json_extract(m.data,'$.role'), p.data "
            "FROM message m JOIN part p ON p.message_id=m.id "
            "WHERE m.session_id=? AND json_extract(p.data,'$.type')='text' "
            "ORDER BY m.time_created",
            (sid,),
        )
        all_msgs = []
        for role, pdata in cur.fetchall():
            try:
                p = json.loads(pdata)
                text = (p.get("text") or "").strip()[:500]
                if text:
                    all_msgs.append({"role": role or "user", "text": text})
            except (json.JSONDecodeError, TypeError):
                continue

        excerpt = list(all_msgs[:3])
        seen = {m["text"] for m in excerpt}
        for m in all_msgs[-6:]:
            if m["text"] not in seen:
                excerpt.append(m)
                seen.add(m["text"])

        task_summary = title or ""
        if not task_summary:
            for m in all_msgs:
                if m["role"] == "user":
                    task_summary = m["text"][:300]
                    break

        if task_summary and "concise status reporter" in task_summary:
            continue

        alive = bool(t_updated and (now_ms - t_updated) <= ACTIVE_WINDOW_MS)
        started_iso = datetime.fromtimestamp(t_created / 1000, tz=timezone.utc).isoformat() if t_created else ""
        last_iso = datetime.fromtimestamp(t_updated / 1000, tz=timezone.utc).isoformat() if t_updated else ""

        sessions.append({
            "sessionId": sid,
            "pid": None,
            "cwd": cwd or "",
            "project": os.path.basename(cwd) if cwd else "",
            "startedAt": started_iso,
            "lastActivity": last_iso,
            "kind": "opencode",
            "entrypoint": "opencode",
            "alive": alive,
            "taskSummary": task_summary[:300],
            "messageCount": msg_count,
            "tokenUsage": {"totalInputTokens": 0, "totalOutputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0},
            "conversationExcerpt": excerpt,
            "model": model_id,
            "agent": agent or "",
        })
    conn.close()
    return sessions


def _find_jsonl(session_id, projects_dir):
    """Find the JSONL conversation file for a session."""
    if not projects_dir or not projects_dir.exists():
        return None
    for jsonl in projects_dir.rglob(f"{session_id}.jsonl"):
        return jsonl
    return None




def _pid_alive_local(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False



def _extract_text_content(content):
    """Extract plain text from a message content field."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    texts.append(c.get("text", ""))
                elif c.get("type") == "tool_result":
                    # Skip tool results to save tokens
                    pass
        return " ".join(texts).strip()
    return ""


def _parse_jsonl(path, started_at):
    """Parse a JSONL file to extract messages, activity info, and conversation excerpts."""
    task_summary = ""
    last_ts = started_at
    message_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    # Collect conversation messages for AI summarization
    first_messages = []  # first few user messages (the task)
    recent_messages = []  # rolling window of recent messages

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = entry.get("type")
                role = entry.get("message", {}).get("role", msg_type)

                # Count user/assistant messages
                if role in ("user", "assistant"):
                    message_count += 1

                # Track token usage from assistant messages
                if role == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    total_input_tokens += usage.get("input_tokens", 0)
                    total_output_tokens += usage.get("output_tokens", 0)
                    cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                    cache_read_tokens += usage.get("cache_read_input_tokens", 0)

                # Extract text content for conversation excerpt
                if role in ("user", "assistant"):
                    text = _extract_text_content(entry.get("message", {}).get("content", ""))
                    if text and not text.startswith("<"):
                        msg_entry = {"role": role, "text": text[:500]}
                        # Keep first 3 messages (task context)
                        if len(first_messages) < 3:
                            first_messages.append(msg_entry)
                        # Keep last 6 messages (recent progress)
                        recent_messages.append(msg_entry)
                        if len(recent_messages) > 6:
                            recent_messages.pop(0)

                # Extract first user message as raw task summary fallback
                if not task_summary and msg_type == "user":
                    text = _extract_text_content(entry.get("message", {}).get("content", ""))
                    if text and not text.startswith("<"):
                        task_summary = text[:300]

                # Track last activity via timestamps in snapshot entries
                snapshot = entry.get("snapshot", {})
                if snapshot:
                    ts_str = snapshot.get("timestamp", "")
                    if ts_str:
                        try:
                            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            ts_ms = int(dt.timestamp() * 1000)
                            if ts_ms > last_ts:
                                last_ts = ts_ms
                        except (ValueError, OSError):
                            pass
    except OSError:
        pass

    # Also check file modification time as a proxy for last activity
    try:
        mtime_ms = int(path.stat().st_mtime * 1000)
        if mtime_ms > last_ts:
            last_ts = mtime_ms
    except OSError:
        pass

    # Build conversation excerpt: first messages + ... + recent messages (deduplicated)
    excerpt = list(first_messages)
    first_texts = {m["text"] for m in first_messages}
    for m in recent_messages:
        if m["text"] not in first_texts:
            excerpt.append(m)

    token_usage = {
        "totalInputTokens": total_input_tokens,
        "totalOutputTokens": total_output_tokens,
        "cacheCreationTokens": cache_creation_tokens,
        "cacheReadTokens": cache_read_tokens,
    }
    return task_summary, last_ts, message_count, excerpt, token_usage


# ---------------------------------------------------------------------------
# Remote collection via SSH
# ---------------------------------------------------------------------------

def _load_ssh_config():
    """Load and parse ~/.ssh/config if it exists."""
    ssh_config = paramiko.SSHConfig()
    ssh_config_path = os.path.expanduser("~/.ssh/config")
    if os.path.exists(ssh_config_path):
        with open(ssh_config_path) as f:
            ssh_config.parse(f)
    return ssh_config


def collect_remote(server_conf, provider="claude"):
    """Collect sessions from a remote server via SSH."""
    host = server_conf["host"]
    label = server_conf.get("label", host)

    script = _remote_script(provider)

    # Shell out to the system `ssh` binary so we honor the user's full SSH config
    # (ControlMaster sockets, IdentityAgent, ProxyCommand, etc). paramiko can't do
    # ControlMaster, which means hardware-backed keys (Secretive, Yubikey) would
    # prompt for confirmation on every poll. With system ssh + ControlPersist,
    # the user confirms once and the multiplex socket handles all later polls.
    if host.startswith("-") or any(c in host for c in " \t\n\r\x00"):
        return [{"server": label, "error": f"invalid host: {host!r}"}]
    user = server_conf.get("user")
    if user and (user.startswith("-") or any(c in user for c in " \t\n\r\x00@")):
        return [{"server": label, "error": f"invalid user: {user!r}"}]
    target = f"{user}@{host}" if user else host

    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if server_conf.get("port"):
        cmd.extend(["-p", str(int(server_conf["port"]))])
    if server_conf.get("hostname"):
        cmd.extend(["-o", f"HostName={server_conf['hostname']}"])
    if server_conf.get("key"):
        cmd.extend(["-i", os.path.expanduser(server_conf["key"])])
    cmd.append("--")  # stop option parsing; defend against `-`-prefixed targets
    cmd.append(target)
    cmd.append(f"python3 -c {_shell_quote(script)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            last_line = stderr.splitlines()[-1] if stderr else f"ssh exit {result.returncode}"
            return [{"server": label, "error": last_line}]

        output = result.stdout
        if not output.strip():
            return []

        sessions = json.loads(output)
        for s in sessions:
            s["server"] = label
        return sessions

    except subprocess.TimeoutExpired:
        return [{"server": label, "error": "SSH timeout (45s)"}]
    except Exception as e:
        print(f"[WARN] Failed to collect from {label} ({host}): {e}")
        return [{"server": label, "error": str(e)}]


def _ssh_has_control_master(server_conf):
    """Return True if `ssh -O check <target>` reports an active multiplex socket."""
    host = server_conf.get("host", "")
    if not host or host.startswith("-") or any(c in host for c in " \t\n\r\x00"):
        return False
    user = server_conf.get("user")
    if user and (user.startswith("-") or any(c in user for c in " \t\n\r\x00@")):
        return False
    target = f"{user}@{host}" if user else host

    cmd = ["ssh", "-O", "check"]
    if server_conf.get("port"):
        cmd.extend(["-p", str(int(server_conf["port"]))])
    if server_conf.get("hostname"):
        cmd.extend(["-o", f"HostName={server_conf['hostname']}"])
    cmd.append("--")
    cmd.append(target)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _collect_group(servers, provider, group_label):
    """Try the servers in a group; return the first non-error result.

    Order: members with an active ControlMaster socket first (no touch
    prompt, instant), then the rest in user-defined order. Falls back to
    the next member only when SSH itself fails — an empty session list
    from a working host counts as success.
    """
    ordered = sorted(servers, key=lambda s: 0 if _ssh_has_control_master(s) else 1)
    last_error = None
    for srv in ordered:
        result = collect_remote(srv, provider)
        errors = [r.get("error") for r in result if "error" in r]
        if not errors:
            for s in result:
                s["server"] = group_label
            return result
        last_error = errors[0]
    return [{"server": group_label, "error": last_error or "no members"}]


def _shell_quote(s):
    """Shell-quote a string for remote execution."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _remote_script(provider):
    """Python script to run on remote servers to collect session data."""
    if provider == "opencode":
        return r'''
import json, os, sys, time, sqlite3
from pathlib import Path
from datetime import datetime, timezone

ACTIVE_WINDOW_MS = 5 * 60 * 1000
MAX = 100

home = Path.home()
db_path = home / ".local" / "share" / "opencode" / "opencode.db"
if not db_path.exists():
    print("[]")
    sys.exit(0)

try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
except sqlite3.Error:
    print("[]")
    sys.exit(0)

cur = conn.cursor()
rows = cur.execute(
    "SELECT id, title, model, agent, directory, time_created, time_updated "
    "FROM session WHERE parent_id IS NULL AND time_archived IS NULL "
    "ORDER BY time_updated DESC LIMIT ?",
    (MAX,),
).fetchall()

now_ms = int(time.time() * 1000)
sessions = []
for sid, title, model_json, agent, cwd, t_created, t_updated in rows:
    try:
        model = json.loads(model_json) if model_json else {}
    except (json.JSONDecodeError, TypeError):
        model = {}
    model_id = model.get("id") or ""

    msg_count = cur.execute(
        "SELECT count(*) FROM message WHERE session_id=?", (sid,)
    ).fetchone()[0]

    cur.execute(
        "SELECT json_extract(m.data,'$.role'), p.data "
        "FROM message m JOIN part p ON p.message_id=m.id "
        "WHERE m.session_id=? AND json_extract(p.data,'$.type')='text' "
        "ORDER BY m.time_created",
        (sid,),
    )
    all_msgs = []
    for role, pdata in cur.fetchall():
        try:
            p = json.loads(pdata)
            text = (p.get("text") or "").strip()[:500]
            if text:
                all_msgs.append({"role": role or "user", "text": text})
        except (json.JSONDecodeError, TypeError):
            continue

    excerpt = list(all_msgs[:3])
    seen = {m["text"] for m in excerpt}
    for m in all_msgs[-6:]:
        if m["text"] not in seen:
            excerpt.append(m)
            seen.add(m["text"])

    task = title or ""
    if not task:
        for m in all_msgs:
            if m["role"] == "user":
                task = m["text"][:300]
                break
    if task and "concise status reporter" in task:
        continue

    alive = bool(t_updated and (now_ms - t_updated) <= ACTIVE_WINDOW_MS)
    started_iso = datetime.fromtimestamp(t_created/1000, tz=timezone.utc).isoformat() if t_created else ""
    last_iso = datetime.fromtimestamp(t_updated/1000, tz=timezone.utc).isoformat() if t_updated else ""

    sessions.append({
        "sessionId": sid,
        "pid": None,
        "cwd": cwd or "",
        "project": os.path.basename(cwd) if cwd else "",
        "startedAt": started_iso,
        "lastActivity": last_iso,
        "kind": "opencode",
        "entrypoint": "opencode",
        "alive": alive,
        "taskSummary": task[:300],
        "messageCount": msg_count,
        "tokenUsage": {"totalInputTokens": 0, "totalOutputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0},
        "conversationExcerpt": excerpt,
        "model": model_id,
        "agent": agent or "",
    })

print(json.dumps(sessions))
'''

    if provider == "codex":
        return r'''
import json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone

ACTIVE_WINDOW_MS = 5 * 60 * 1000
MAX_SESSIONS = 100

def parse_iso_ms(ts):
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0

def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"):
                texts.append(c.get("text", ""))
        return " ".join(t for t in texts if t).strip()
    return ""

def parse_session(path):
    session_id = ""
    cwd = ""
    started_at = 0
    task_summary = ""
    last_ts = 0
    message_count = 0
    first_msgs = []
    recent_msgs = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                ts_ms = parse_iso_ms(entry.get("timestamp", ""))
                if ts_ms > last_ts:
                    last_ts = ts_ms

                if entry.get("type") == "session_meta":
                    payload = entry.get("payload", {})
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd", cwd)
                    started_at = parse_iso_ms(payload.get("timestamp", "")) or started_at
                    continue

                if entry.get("type") != "response_item":
                    continue
                payload = entry.get("payload", {})
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                message_count += 1
                text = extract_text(payload.get("content", ""))
                if not text or text.startswith("<"):
                    continue
                me = {"role": role, "text": text[:500]}
                if len(first_msgs) < 3:
                    first_msgs.append(me)
                recent_msgs.append(me)
                if len(recent_msgs) > 6:
                    recent_msgs.pop(0)
                if not task_summary and role == "user":
                    task_summary = text[:300]
    except Exception:
        return None

    try:
        mtime_ms = int(path.stat().st_mtime * 1000)
        if mtime_ms > last_ts:
            last_ts = mtime_ms
    except Exception:
        pass

    if not started_at:
        started_at = last_ts

    if task_summary and "concise status reporter" in task_summary:
        return None

    first_texts = {m["text"] for m in first_msgs}
    excerpt = list(first_msgs)
    for m in recent_msgs:
        if m["text"] not in first_texts:
            excerpt.append(m)

    now_ms = int(time.time() * 1000)
    alive = bool(last_ts and (now_ms - last_ts) <= ACTIVE_WINDOW_MS)
    sid = session_id or path.stem
    started_dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat() if started_at else ""
    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).isoformat() if last_ts else ""

    return {
        "sessionId": sid,
        "pid": None,
        "cwd": cwd,
        "project": os.path.basename(cwd) if cwd else "",
        "startedAt": started_dt,
        "lastActivity": last_dt,
        "kind": "codex",
        "entrypoint": "codex",
        "alive": alive,
        "taskSummary": task_summary,
        "messageCount": message_count,
        "tokenUsage": {"totalInputTokens": 0, "totalOutputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0},
        "conversationExcerpt": excerpt,
    }

home = Path.home()
sessions_dir = home / ".codex" / "sessions"
if not sessions_dir.exists():
    print("[]")
    sys.exit(0)

files = sorted(
    sessions_dir.rglob("*.jsonl"),
    key=lambda p: p.stat().st_mtime if p.exists() else 0,
    reverse=True,
)[:MAX_SESSIONS]

results = []
for fp in files:
    session = parse_session(fp)
    if session:
        results.append(session)

print(json.dumps(results))
'''

    return r'''
import json, os, sys
from pathlib import Path
from datetime import datetime, timezone

def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(c.get("text","") for c in content if isinstance(c,dict) and c.get("type")=="text").strip()
    return ""

home = Path.home()
sessions_dir = home / ".claude" / "sessions"
projects_dir = home / ".claude" / "projects"

if not sessions_dir.exists():
    print("[]")
    sys.exit(0)

results = []
for sf in sessions_dir.glob("*.json"):
    try:
        meta = json.loads(sf.read_text())
    except Exception:
        continue

    session_id = meta.get("sessionId", "")
    if not session_id or session_id.startswith("agent-"):
        continue

    pid = meta.get("pid")
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except Exception:
            pass

    cwd = meta.get("cwd", "")
    started_at = meta.get("startedAt", 0)

    # Find and parse JSONL
    task_summary = ""
    message_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    last_activity = started_at
    first_msgs = []
    recent_msgs = []
    jsonl_path = None
    if projects_dir.exists():
        for jp in projects_dir.rglob(f"{session_id}.jsonl"):
            jsonl_path = jp
            break

    if jsonl_path:
        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    msg_type = entry.get("type")
                    role = entry.get("message", {}).get("role", msg_type)
                    if role in ("user", "assistant"):
                        message_count += 1
                    if role == "assistant":
                        usage = entry.get("message", {}).get("usage", {})
                        total_input_tokens += usage.get("input_tokens", 0)
                        total_output_tokens += usage.get("output_tokens", 0)
                        cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                        cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                    if role in ("user", "assistant"):
                        text = extract_text(entry.get("message", {}).get("content", ""))
                        if text and not text.startswith("<"):
                            me = {"role": role, "text": text[:500]}
                            if len(first_msgs) < 3:
                                first_msgs.append(me)
                            recent_msgs.append(me)
                            if len(recent_msgs) > 6:
                                recent_msgs.pop(0)
                    if not task_summary and msg_type == "user":
                        text = extract_text(entry.get("message", {}).get("content", ""))
                        if text and not text.startswith("<"):
                            task_summary = text[:300]
            mtime_ms = int(jsonl_path.stat().st_mtime * 1000)
            if mtime_ms > last_activity:
                last_activity = mtime_ms
        except Exception:
            pass

    if task_summary and "concise status reporter" in task_summary:
        continue

    excerpt = list(first_msgs)
    first_texts = {m["text"] for m in first_msgs}
    for m in recent_msgs:
        if m["text"] not in first_texts:
            excerpt.append(m)

    started_dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat() if started_at else ""
    last_dt = ""
    if last_activity and last_activity != started_at:
        last_dt = datetime.fromtimestamp(last_activity / 1000, tz=timezone.utc).isoformat()

    results.append({
        "sessionId": session_id,
        "pid": pid,
        "cwd": cwd,
        "project": os.path.basename(cwd) if cwd else "",
        "startedAt": started_dt,
        "lastActivity": last_dt,
        "kind": meta.get("kind", ""),
        "entrypoint": meta.get("entrypoint", ""),
        "alive": alive,
        "taskSummary": task_summary,
        "messageCount": message_count,
        "tokenUsage": {
            "totalInputTokens": total_input_tokens,
            "totalOutputTokens": total_output_tokens,
            "cacheCreationTokens": cache_creation_tokens,
            "cacheReadTokens": cache_read_tokens,
        },
        "conversationExcerpt": excerpt,
    })

print(json.dumps(results))
'''


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _collect_all():
    """Collect sessions from all configured (source, provider) pairs in parallel.

    Servers sharing a `group` value are treated as equivalent login nodes —
    we try one member per (group, provider) and stop on the first success.
    """
    config = load_config()
    providers = config.get("providers") or ["claude"]
    all_sessions = []

    # Bucket: groups maps group_name -> [server_conf, ...] preserving config order;
    # ungrouped is the list of standalone servers.
    groups = {}
    ungrouped = []
    for srv in config.get("servers", []):
        g = srv.get("group")
        if isinstance(g, str) and g.strip():
            groups.setdefault(g.strip(), []).append(srv)
        else:
            ungrouped.append(srv)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {}

        if config.get("include_local", True):
            for p in providers:
                futures[pool.submit(collect_local, p)] = ("local", p)

        for srv in ungrouped:
            for p in providers:
                futures[pool.submit(collect_remote, srv, p)] = (srv.get("label", srv["host"]), p)

        for group_name, group_servers in groups.items():
            for p in providers:
                futures[pool.submit(_collect_group, group_servers, p, group_name)] = (group_name, p)

        for future in as_completed(futures):
            label, p = futures[future]
            try:
                result = future.result()
                for s in result:
                    s.setdefault("provider", p)
                all_sessions.extend(result)
            except Exception as e:
                all_sessions.append({"server": label, "error": str(e), "provider": p})

    return all_sessions


def _get_cached():
    """Return cached data or refresh if stale."""
    with _cache["lock"]:
        now = time.time()
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]

    data = _collect_all()

    with _cache["lock"]:
        _cache["data"] = data
        _cache["ts"] = time.time()

    return data


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force cache invalidation and re-collect."""
    with _cache["lock"]:
        _cache["data"] = None
        _cache["ts"] = 0
    return api_sessions()


# ---------------------------------------------------------------------------
# Server configuration API
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config_get():
    """Return current configuration."""
    config = load_config()
    return jsonify(config)


@app.route("/api/config", methods=["PUT"])
def api_config_put():
    """Replace the entire configuration."""
    config = request.get_json()
    if not config:
        return jsonify({"error": "Invalid JSON"}), 400
    save_config(config)
    _invalidate_cache()
    return jsonify(config)


@app.route("/api/servers", methods=["GET"])
def api_servers_list():
    config = load_config()
    servers = config.get("servers", [])
    # Add an index to each server for identification
    for i, s in enumerate(servers):
        s["id"] = i
    return jsonify({
        "servers": servers,
        "include_local": config.get("include_local", True),
        "providers": config.get("providers") or ["claude"],
        "available_providers": [{"key": k, "label": v["label"]} for k, v in PROVIDERS.items()],
    })


@app.route("/api/servers", methods=["POST"])
def api_servers_add():
    """Add a new server."""
    server = request.get_json()
    if not server or not server.get("host"):
        return jsonify({"error": "host is required"}), 400

    entry = {}
    for key in ("host", "port", "user", "key", "label", "group"):
        if server.get(key):
            val = server[key]
            entry[key] = int(val) if key == "port" else val

    config = load_config()
    config.setdefault("servers", []).append(entry)
    save_config(config)
    _invalidate_cache()
    return jsonify({"ok": True, "index": len(config["servers"]) - 1}), 201


@app.route("/api/servers/<int:idx>", methods=["PUT"])
def api_servers_update(idx):
    """Update a server by index."""
    server = request.get_json()
    if not server:
        return jsonify({"error": "Invalid JSON"}), 400

    config = load_config()
    servers = config.get("servers", [])
    if idx < 0 or idx >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    entry = {}
    for key in ("host", "port", "user", "key", "label", "group"):
        if server.get(key):
            val = server[key]
            entry[key] = int(val) if key == "port" else val

    servers[idx] = entry
    config["servers"] = servers
    save_config(config)
    _invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/servers/<int:idx>", methods=["DELETE"])
def api_servers_delete(idx):
    """Remove a server by index."""
    config = load_config()
    servers = config.get("servers", [])
    if idx < 0 or idx >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    servers.pop(idx)
    config["servers"] = servers
    save_config(config)
    _invalidate_cache()
    return jsonify({"ok": True})


@app.route("/api/config/local", methods=["PUT"])
def api_config_local():
    """Toggle include_local setting."""
    body = request.get_json()
    config = load_config()
    config["include_local"] = bool(body.get("include_local", True))
    save_config(config)
    _invalidate_cache()
    return jsonify({"ok": True, "include_local": config["include_local"]})


@app.route("/api/config/providers", methods=["PUT"])
def api_config_providers():
    """Set the active list of session providers."""
    body = request.get_json() or {}
    providers = _normalize_providers(body.get("providers", body.get("provider")))
    if not providers:
        providers = ["claude"]
    config = load_config()
    config["providers"] = providers
    save_config(config)
    _invalidate_cache()
    return jsonify({"ok": True, "providers": providers})


@app.route("/api/servers/<int:idx>/test", methods=["POST"])
def api_servers_test(idx):
    """Test SSH connection to a server for each currently-selected provider."""
    config = load_config()
    providers = config.get("providers") or ["claude"]
    servers = config.get("servers", [])
    if idx < 0 or idx >= len(servers):
        return jsonify({"error": "Server not found"}), 404

    srv = servers[idx]
    per_provider = []
    for p in providers:
        try:
            result = collect_remote(srv, p)
            err = next((r.get("error") for r in result if "error" in r), None)
            if err:
                per_provider.append({"provider": p, "ok": False, "error": err})
            else:
                per_provider.append({"provider": p, "ok": True, "sessions": len(result)})
        except Exception as e:
            per_provider.append({"provider": p, "ok": False, "error": str(e)})

    overall_ok = all(r["ok"] for r in per_provider)
    total_sessions = sum(r.get("sessions", 0) for r in per_provider if r["ok"])
    return jsonify({
        "ok": overall_ok,
        "sessions": total_sessions,
        "results": per_provider,
        # Legacy single-error field for older UIs
        "error": next((r["error"] for r in per_provider if not r["ok"]), None),
    })


def _invalidate_cache():
    with _cache["lock"]:
        _cache["data"] = None
        _cache["ts"] = 0


# ---------------------------------------------------------------------------
# AI Summarization via local CLI (Claude/Codex)
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """You are a concise status reporter. Given a conversation excerpt from an AI coding agent session, produce a brief JSON summary.

The session is working in: {cwd}
The session is currently: {status}
Agent provider: {provider}

Conversation excerpt:
{conversation}

Respond with ONLY a JSON object (no markdown fencing):
{{
  "task": "<1-sentence: what is the agent working on>",
  "progress": "<1-sentence: current progress/status>",
  "percent": <estimated completion percentage 0-100, or null if unclear>
}}

Use the same language as the conversation (e.g. if the user speaks Chinese, respond in Chinese).
Be specific and concise."""


def _summarize_session(session):
    """Call local CLI (claude/codex) to summarize a single session."""
    excerpt = session.get("conversationExcerpt", [])
    if not excerpt:
        return None

    conversation_text = "\n".join(
        f"[{m['role']}]: {m['text']}" for m in excerpt
    )

    status = "RUNNING" if session.get("alive") else "COMPLETED"
    provider = session.get("provider", "claude")
    prompt = SUMMARIZE_PROMPT.format(
        cwd=session.get("cwd", "unknown"),
        status=status,
        provider=provider,
        conversation=conversation_text,
    )

    try:
        if provider == "codex":
            cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json"]
            if SUMMARIZER_MODEL_CODEX:
                cmd.extend(["--model", SUMMARIZER_MODEL_CODEX])
            cmd.append(prompt)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        else:
            result = subprocess.run(
                ["claude", "-p", "--model", SUMMARIZER_MODEL_CLAUDE, prompt],
                capture_output=True,
                text=True,
                timeout=30,
            )

        if result.returncode != 0:
            print(f"[WARN] summarize CLI failed for {session.get('sessionId', '?')}: {result.stderr[:200]}")
            return _fallback_summary(session)

        text = result.stdout.strip()
        if provider == "codex":
            # codex --json can emit different JSONL event shapes; extract assistant text robustly.
            text = _extract_codex_exec_message(text) or text

        parsed = _parse_summary_json(text)
        if not parsed.get("task") and not parsed.get("progress"):
            return _fallback_summary(session)
        return parsed
    except subprocess.TimeoutExpired:
        print(f"[WARN] summarize CLI timed out for {session.get('sessionId', '?')}")
        return _fallback_summary(session)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[WARN] Summarize failed for {session.get('sessionId', '?')}: {e}")
        return _fallback_summary(session)


def _extract_first_json_object(text):
    """Extract first JSON object from arbitrary text."""
    text = (text or "").strip()
    if not text:
        return ""

    # Prefer fenced block content if present.
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[i:])
            return text[i:i + end]
        except json.JSONDecodeError:
            continue
    return text


def _extract_codex_exec_message(jsonl_text):
    """Extract assistant message text from codex exec --json output."""
    last_msg = ""
    for line in (jsonl_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Format A:
        # {"id":"0","msg":{"type":"agent_message","message":"..."}}
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            message = msg.get("message")
            if isinstance(message, str) and message.strip():
                last_msg = message.strip()
                continue

        # Format B:
        # {"type":"response_item","payload":{"type":"message","role":"assistant","content":[...]}}
        if event.get("type") == "response_item":
            payload = event.get("payload", {})
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                parsed = _extract_codex_text_content(payload.get("content", []))
                if parsed:
                    last_msg = parsed

        # Format C (newer Codex JSON stream):
        # {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    last_msg = text.strip()
    return last_msg


def _parse_summary_json(text):
    """Parse summary JSON with tolerance for extra content."""
    candidate = _extract_first_json_object(text)
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("summary is not a JSON object", candidate, 0)
    task = data.get("task")
    progress = data.get("progress")
    percent = data.get("percent")
    if not isinstance(task, str):
        task = ""
    if progress is not None and not isinstance(progress, str):
        progress = str(progress)
    if percent is not None:
        try:
            percent = max(0, min(100, int(percent)))
        except (ValueError, TypeError):
            percent = None
    return {"task": task, "progress": progress, "percent": percent}


def _fallback_summary(session):
    """Best-effort local summary when external CLI summarization fails."""
    excerpt = session.get("conversationExcerpt", []) or []
    task = (session.get("taskSummary") or "").strip()
    if not task:
        for m in excerpt:
            if m.get("role") == "user" and m.get("text"):
                task = m["text"].strip()
                break
    if not task:
        task = "Session is active"

    progress = ""
    for m in reversed(excerpt):
        if m.get("role") == "assistant" and m.get("text"):
            progress = m["text"].strip()
            break
    if not progress and excerpt:
        progress = excerpt[-1].get("text", "").strip()
    if not progress:
        progress = "No recent progress message available"

    return {
        "task": task[:180],
        "progress": progress[:220],
        "percent": None,
    }


def _summary_cache_key(session):
    return f"{session.get('provider', 'claude')}:{session.get('server', 'local')}:{session.get('sessionId', '')}"


_summarize_executor = ThreadPoolExecutor(max_workers=3)
_summarize_in_flight = set()  # session IDs currently being summarized


def _on_summary_done(session_id, message_count, future):
    """Callback when a background summary completes."""
    _summarize_in_flight.discard(session_id)
    try:
        result = future.result()
        if result:
            with _summary_cache["lock"]:
                _summary_cache["data"][session_id] = {
                    "messageCount": message_count,
                    "summary": result,
                }
                _save_summary_cache()
    except Exception as e:
        print(f"[WARN] Summary future failed for {session_id}: {e}")


def _summarize_all(sessions):
    """Apply cached summaries and kick off background summarization for uncached ones.

    Non-blocking: sessions without a cached summary get one on the next refresh.
    """
    for s in sessions:
        if "error" in s:
            continue
        sid = _summary_cache_key(s)
        mc = s.get("messageCount", 0)

        with _summary_cache["lock"]:
            cached = _summary_cache["data"].get(sid)

        if cached and cached.get("messageCount") == mc:
            s["aiSummary"] = cached["summary"]
        else:
            # Keep the old summary visible while regenerating
            if cached:
                s["aiSummary"] = cached["summary"]
            if sid not in _summarize_in_flight and mc > 0:
                _summarize_in_flight.add(sid)
                session_data = {
                    "sessionId": s.get("sessionId", sid),
                    "cwd": s.get("cwd", ""),
                    "alive": s.get("alive", False),
                    "provider": s.get("provider", "claude"),
                    "conversationExcerpt": s.get("conversationExcerpt", []),
                }
                fut = _summarize_executor.submit(_summarize_session, session_data)
                fut.add_done_callback(lambda f, sid=sid, mc=mc: _on_summary_done(sid, mc, f))


@app.route("/api/sessions")
def api_sessions():
    # Claude collection returns active sessions only, while Codex collection can
    # also include inactive session logs so we can surface them as completed.
    sessions = _get_cached()

    # Run AI summarization
    _summarize_all(sessions)

    # Detect running→completed transitions
    current_running_ids = set()
    running = []
    errors = []

    for s in sessions:
        if "error" in s:
            errors.append(s)
            continue
        sid = s.get("sessionId", "")
        # Build a copy without conversationExcerpt for the response
        clean = {k: v for k, v in s.items() if k != "conversationExcerpt"}

        if s.get("alive"):
            current_running_ids.add(sid)
            running.append(clean)
        else:
            clean["alive"] = False

        # Track in known_sessions; remove from completed if resumed
        with _known_sessions["lock"]:
            if s.get("alive"):
                _known_sessions["running"][sid] = clean
                _known_sessions["completed"].pop(sid, None)
            else:
                _known_sessions["running"].pop(sid, None)
                _known_sessions["completed"][sid] = clean

    # Sessions that were running before but are no longer → completed
    with _known_sessions["lock"]:
        gone_ids = set(_known_sessions["running"].keys()) - current_running_ids
        for sid in gone_ids:
            s = _known_sessions["running"].pop(sid)
            s["alive"] = False
            _known_sessions["completed"][sid] = s

        completed = list(_known_sessions["completed"].values())

    running.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
    completed.sort(key=lambda x: x.get("lastActivity", "") or x.get("startedAt", ""), reverse=True)

    return jsonify({
        "running": running,
        "completed": completed,
        "errors": errors,
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "cacheTTL": CACHE_TTL,
    })


@app.route("/api/sessions/<session_id>/dismiss", methods=["POST"])
def api_session_dismiss(session_id):
    """Remove a completed session from the board."""
    with _known_sessions["lock"]:
        if session_id in _known_sessions["completed"]:
            del _known_sessions["completed"][session_id]
            return jsonify({"ok": True})
    return jsonify({"error": "Session not found in completed"}), 404


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Code Agent Kanban")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", "-p", type=int, default=5555, help="Bind port (default: 5555)")
    args = parser.parse_args()

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _load_summary_cache()
    print(f"Code Agent Kanban running at http://localhost:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
