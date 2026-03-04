#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0"]
# ///
"""
pi-boss: a CLI that governs multiple pi coding agent instances via RPC mode.

Usage:
    pi-boss "build a web scraper"              # Start new work or query existing
    echo "build a web scraper" | pi-boss       # Same, via stdin
    pi-boss status                             # Show all managed sessions
    pi-boss status <pattern>                   # Filter to matching sessions
    pi-boss dump <pattern>                     # Dump session transcript
    pi-boss stop <pattern>                     # Cancel a running session
    pi-boss append <pattern> "more instructions"  # Continue an existing session with new input

Options:
    --group <id>              Group ID for supersede behavior (see below).
    --debounce <seconds>      Debounce window in seconds (default: 5, 0 to disable).
    --llm <model>             Model for title generation (default: gpt-4o-mini).
    --cwd <dir>               Working directory for the pi session.

Concurrency & safety:

    pi-boss is designed to be called rapidly and concurrently — e.g. from a
    voice interface that may fire multiple times as speech is refined.

    All instruction requests are serialized via a file lock. Before starting
    new work, pi-boss automatically cancels any session younger than the
    debounce window (default: 5 seconds). This means rapid-fire calls are
    safe: only the last one within each window actually runs.

    For explicit control, pass --group <id>. Any in-flight session with the
    same group ID is cancelled regardless of age before the new request
    proceeds. Sessions without a group are only subject to the debounce
    window. Two concurrent tasks are fine as long as they're spaced further
    apart than the debounce window, or given different group IDs.

    Cancelled sessions show status "cancelled" in pi-boss status. Workers
    check for cancellation and abort their pi instance promptly.
"""

import sys
import os
import json
import subprocess
import signal
import time
import re
import fcntl
import secrets
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOSS_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BOSS_DIR / "sessions"
BOSS_MAX_TOKENS = 1024
DEFAULT_DEBOUNCE_SECS = 5

# All LLM calls go through OpenRouter.
# Configured via .env file in BOSS_DIR:
#   OPENROUTER_API_KEY          — required
#   OPENROUTER_SMART_MODEL      — boss brain (default: anthropic/claude-haiku-4.5)
#   OPENROUTER_CHEAP_MODEL      — title generation (default: google/gemma-3-27b-it)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_SMART_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_CHEAP_MODEL = "google/gemma-3-27b-it"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_stamp():
    """Timestamp with 4 random hex chars for uniqueness."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(2)
    return f"{ts}-{suffix}"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def slugify(text, max_len=40):
    """Turn text into a short filesystem-safe slug."""
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:max_len].rstrip('-') or "task"

def pid_alive(pid):
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def load_meta(session_dir):
    """Load meta.json from a session directory (tolerates missing/corrupt files)."""
    meta_path = Path(session_dir) / "meta.json"
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

def save_meta(session_dir, meta):
    """Atomically save meta.json (write tmp + rename)."""
    meta_path = Path(session_dir) / "meta.json"
    tmp_path = meta_path.with_suffix('.json.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(meta, f, indent=2)
    os.rename(tmp_path, meta_path)

def get_all_sessions():
    """Get all session directories with their metadata, sorted by time."""
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for d in sorted(SESSIONS_DIR.iterdir()):
        if d.is_dir():
            meta = load_meta(d)
            if meta:
                meta['_dir'] = str(d)
                meta['_name'] = d.name
                sessions.append(meta)
    return sessions

def get_session_status(meta):
    """Determine actual status of a session (running/done/dead)."""
    recorded = meta.get('status', 'unknown')
    if recorded in ('done', 'killed', 'cancelled'):
        return recorded
    pid = meta.get('pid')
    worker_pid = meta.get('worker_pid')
    # Check if either process is alive
    if pid and pid_alive(pid):
        return 'running'
    if worker_pid and pid_alive(worker_pid):
        return 'starting'
    if recorded in ('running', 'starting'):
        return 'dead'
    return recorded

def session_age_secs(meta):
    """How many seconds ago this session was started."""
    try:
        start = datetime.fromisoformat(meta['started_at'])
        return (datetime.now(timezone.utc) - start).total_seconds()
    except Exception:
        return float('inf')

def get_last_assistant_text(session_dir):
    """Extract the last assistant text from events."""
    events_path = Path(session_dir) / "events.jsonl"
    if not events_path.exists():
        return None
    last_text = None
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get('type') == 'message_end':
                    msg = evt.get('message', {})
                    if msg.get('role') == 'assistant':
                        for block in msg.get('content', []):
                            if block.get('type') == 'text':
                                last_text = block.get('text', '')
    except Exception:
        pass
    return last_text

def get_last_tool_activity(session_dir):
    """Get a snippet of the last tool execution."""
    events_path = Path(session_dir) / "events.jsonl"
    if not events_path.exists():
        return None
    last_tool = None
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get('type') == 'tool_execution_end':
                    name = evt.get('toolName', '?')
                    result = evt.get('result', {})
                    text_parts = []
                    for c in result.get('content', []):
                        if c.get('type') == 'text':
                            text_parts.append(c['text'])
                    snippet = '\n'.join(text_parts)[:200]
                    last_tool = f"{name}: {snippet}"
                elif evt.get('type') == 'tool_execution_start':
                    name = evt.get('toolName', '?')
                    args = evt.get('args', {})
                    if name == 'bash':
                        last_tool = f"bash: {args.get('command', '?')[:150]}"
                    elif name in ('edit', 'write', 'read'):
                        last_tool = f"{name}: {args.get('path', '?')}"
                    else:
                        last_tool = f"{name}: ..."
    except Exception:
        pass
    return last_tool

def load_dot_env():
    """Load key=value pairs from BOSS_DIR/.env (if it exists) into a dict."""
    env_path = BOSS_DIR / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

def get_openai_client():
    """Build an OpenAI client pointing at OpenRouter using .env credentials."""
    from openai import OpenAI
    env = load_dot_env()
    api_key = env.get('OPENROUTER_API_KEY', '')
    if not api_key:
        return None
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

def fallback_title(task):
    """Return the first 50 characters of the task as a fallback title."""
    if not task:
        return "Untitled"
    if len(task) <= 50:
        return task
    return task[:50].rstrip() + "…"

def generate_title(task, model=None):
    """Call OpenRouter to generate a concise session title (2-5 words)."""
    env = load_dot_env()
    llm_model = model or env.get('OPENROUTER_CHEAP_MODEL') or DEFAULT_CHEAP_MODEL

    client = get_openai_client()
    if not client:
        print("No OPENROUTER_API_KEY in .env, using fallback title", file=sys.stderr)
        return fallback_title(task)

    try:
        resp = client.chat.completions.create(
            model=llm_model,
            max_tokens=500,  # reasoning models need headroom for chain-of-thought
            messages=[{
                "role": "user",
                "content": (
                    "Write a single short title (3-7 words) for this coding task. "
                    "Output ONLY one title on one line. No alternatives, no list, "
                    f"no quotes, no punctuation.\n\nTask: {task}"
                ),
            }],
        )
        text = (resp.choices[0].message.content or "").strip().split('\n')[0].strip()
        return text if text else fallback_title(task)
    except Exception as e:
        print(f"Title generation failed: {e}", file=sys.stderr)
        return fallback_title(task)

def ensure_title(session_dir, meta, model=None):
    """Ensure a session has a title, generating via LLM once the session is done.

    While a session is still running, returns the slug-derived title from meta.
    Once done/error/dead, upgrades to an LLM-generated title (once) and caches it.
    """
    status = get_session_status(meta)
    slug_title = (meta.get('slug') or '').replace('-', ' ').title()
    current_title = meta.get('title') or slug_title or fallback_title(meta.get('task', ''))

    # Only upgrade to LLM title for finished sessions that still have the slug-derived title
    if status in ('done', 'error', 'dead', 'killed') and current_title == slug_title:
        task = meta.get('task', '')
        title = generate_title(task, model=model)
        meta['title'] = title
        save_meta(session_dir, meta)
        return title

    return current_title

def elapsed_str(started_at):
    """Human-readable elapsed time from ISO timestamp."""
    try:
        start = datetime.fromisoformat(started_at)
        now = datetime.now(timezone.utc)
        secs = int((now - start).total_seconds())
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        else:
            return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return "?"

def truncate(text, max_len=120):
    if not text:
        return ""
    text = text.replace('\n', ' ').strip()
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text

# ---------------------------------------------------------------------------
# File lock (serializes instruction handling)
# ---------------------------------------------------------------------------

class SessionLock:
    """Process-level lock using flock on sessions/.lock."""

    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock_path = SESSIONS_DIR / ".lock"
        self._fd = None

    def acquire(self):
        self._fd = open(self._lock_path, 'w')
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

# ---------------------------------------------------------------------------
# Cancel / supersede logic
# ---------------------------------------------------------------------------

def cancel_session_processes(meta, session_dir):
    """Kill all processes for a session and mark it cancelled."""
    pid = meta.get('pid')
    worker_pid = meta.get('worker_pid')
    for p in [pid, worker_pid]:
        if p and pid_alive(p):
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
    meta['status'] = 'cancelled'
    meta['finished_at'] = now_iso()
    save_meta(session_dir, meta)

def cancel_young_sessions(debounce_secs, group=None):
    """Cancel sessions younger than debounce_secs.

    If group is set, also cancel any session with that group regardless of age.
    Returns list of cancelled session names.
    """
    cancelled = []
    for s in get_all_sessions():
        status = get_session_status(s)
        if status not in ('starting', 'running'):
            continue

        should_cancel = False
        name = s.get('_name', '?')
        sdir = s['_dir']

        # Group match: cancel regardless of age
        if group and s.get('group') == group:
            should_cancel = True

        # Age-based: cancel if younger than debounce window
        if debounce_secs > 0 and session_age_secs(s) < debounce_secs:
            should_cancel = True

        if should_cancel:
            cancel_session_processes(s, sdir)
            cancelled.append(name)

    return cancelled

# ---------------------------------------------------------------------------
# Boss Brain (LLM gateway)
# ---------------------------------------------------------------------------

def call_boss_llm(system_prompt, user_message):
    """Call OpenRouter for boss brain decisions."""
    env = load_dot_env()
    llm_model = env.get('OPENROUTER_SMART_MODEL') or DEFAULT_SMART_MODEL

    client = get_openai_client()
    if not client:
        raise RuntimeError("No OPENROUTER_API_KEY in .env")

    resp = client.chat.completions.create(
        model=llm_model,
        max_tokens=BOSS_MAX_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        timeout=30,
    )
    return (resp.choices[0].message.content or "").strip()

def build_sessions_context():
    """Build a text summary of all sessions for the boss brain."""
    sessions = get_all_sessions()
    if not sessions:
        return "No active or past sessions."

    lines = []
    for s in sessions:
        status = get_session_status(s)
        if status == 'cancelled':
            continue  # Don't clutter context with cancelled sessions
        elapsed = elapsed_str(s.get('started_at', ''))
        task = s.get('task', '?')
        name = s.get('_name', '?')
        last_text = get_last_assistant_text(s['_dir'])
        last_tool = get_last_tool_activity(s['_dir'])

        lines.append(f"SESSION: {name}")
        lines.append(f"  Status: {status} | Elapsed: {elapsed}")
        lines.append(f"  Task: {task}")
        if last_tool:
            lines.append(f"  Last tool: {truncate(last_tool, 200)}")
        if last_text:
            lines.append(f"  Last response: {truncate(last_text, 300)}")
        lines.append("")

    return "\n".join(lines) if lines else "No active or past sessions."

# ---------------------------------------------------------------------------
# Worker mode (backgrounded, manages one pi instance)
# ---------------------------------------------------------------------------

def run_worker(session_dir):
    """Run a pi RPC instance, capture events, update meta on completion."""
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if not meta:
        print(f"No meta.json in {session_dir}", file=sys.stderr)
        sys.exit(1)

    # Check if already cancelled before we even start pi
    if meta.get('status') == 'cancelled':
        sys.exit(0)

    prompt = meta.get('prompt') or meta['task']
    events_path = session_dir / "events.jsonl"

    pi_cmd = [
        "pi", "--mode", "rpc",
        "--session-dir", str(session_dir),
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes",
    ]

    # If resuming, point pi at the existing session file
    session_file = find_session_jsonl(session_dir)
    if meta.get('resuming') and session_file:
        pi_cmd.extend(["--session", str(session_file)])

    work_dir = meta.get('cwd') or str(BOSS_DIR)

    proc = subprocess.Popen(
        pi_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(session_dir / "stderr.log", 'a'),
        cwd=work_dir,
        text=True,
        bufsize=1,
    )

    meta['pid'] = proc.pid
    meta['status'] = 'running'
    for key in ('prompt', 'resuming'):
        meta.pop(key, None)  # consumed; don't re-send on next restart
    save_meta(session_dir, meta)

    prompt_cmd = json.dumps({"type": "prompt", "message": prompt})
    proc.stdin.write(prompt_cmd + "\n")
    proc.stdin.flush()

    agent_done = False
    try:
        with open(events_path, 'a') as ef:
            for line in proc.stdout:
                # Check if we've been cancelled mid-run
                current_meta = load_meta(session_dir)
                if current_meta and current_meta.get('status') == 'cancelled':
                    try:
                        abort_cmd = json.dumps({"type": "abort"})
                        proc.stdin.write(abort_cmd + "\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                    break

                line = line.strip()
                if not line:
                    continue
                ef.write(line + "\n")
                ef.flush()

                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if evt.get('type') == 'agent_end':
                    agent_done = True
                    break

                if evt.get('type') == 'extension_ui_request':
                    method = evt.get('method', '')
                    eid = evt.get('id', '')
                    if method in ('select', 'confirm', 'input', 'editor'):
                        cancel = json.dumps({
                            "type": "extension_ui_response",
                            "id": eid,
                            "cancelled": True
                        })
                        proc.stdin.write(cancel + "\n")
                        proc.stdin.flush()

    except Exception as e:
        with open(session_dir / "worker_error.log", 'w') as f:
            f.write(str(e))

    try:
        proc.stdin.close()
    except Exception:
        pass

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Only update status if we weren't already cancelled externally
    current_meta = load_meta(session_dir)
    if current_meta and current_meta.get('status') != 'cancelled':
        current_meta['status'] = 'done' if agent_done else 'error'
        current_meta['finished_at'] = now_iso()
        current_meta['exit_code'] = proc.returncode
        save_meta(session_dir, current_meta)

# ---------------------------------------------------------------------------
# Start a new task
# ---------------------------------------------------------------------------

def start_task(slug, task, group=None, cwd=None):
    """Create a session and launch a background worker."""
    session_name = f"{now_stamp()}-{slug}"
    session_dir = SESSIONS_DIR / session_name
    session_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task": task,
        "slug": slug,
        "title": slug.replace('-', ' ').title(),
        "status": "starting",
        "started_at": now_iso(),
        "pid": None,
        "worker_pid": None,
        "finished_at": None,
        "exit_code": None,
    }
    if group:
        meta["group"] = group
    if cwd:
        meta["cwd"] = str(Path(cwd).resolve())
    save_meta(session_dir, meta)

    worker_cmd = ["uv", "run", __file__, "--worker", str(session_dir)]
    proc = subprocess.Popen(
        worker_cmd,
        stdin=subprocess.DEVNULL,
        stdout=open(session_dir / "worker_stdout.log", 'w'),
        stderr=open(session_dir / "worker_stderr.log", 'w'),
        start_new_session=True,
    )

    meta['worker_pid'] = proc.pid
    save_meta(session_dir, meta)

    return session_name

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status(pattern=None, as_json=False, full=False, llm_model=None, limit=None):
    """Print status of all (or matching) sessions, newest first."""
    sessions = get_all_sessions()
    sessions.reverse()  # newest first

    if pattern:
        sessions = [s for s in sessions if pattern in s.get('_name', '') or pattern in s.get('task', '')]

    if limit is not None:
        sessions = sessions[:limit]

    if as_json:
        out = []
        for s in sessions:
            status = get_session_status(s)
            title = ensure_title(s['_dir'], s, model=llm_model)
            entry = {
                "name": s.get('_name'),
                "task": s.get('task'),
                "title": title,
                "status": status,
                "pid": s.get('pid'),
                "started_at": s.get('started_at'),
                "finished_at": s.get('finished_at'),
                "elapsed": elapsed_str(s.get('started_at', '')),
            }
            if s.get('group'):
                entry["group"] = s['group']
            if status != 'cancelled':
                last_tool = get_last_tool_activity(s['_dir'])
                if last_tool:
                    entry["last_tool"] = last_tool
                last_text = get_last_assistant_text(s['_dir'])
                if last_text:
                    entry["last_response"] = last_text
            out.append(entry)
        print(json.dumps(out, indent=2))
        return

    if not sessions:
        print(f"No sessions matching '{pattern}'." if pattern else "No sessions found.")
        return

    t = (lambda text, n: text) if full else truncate

    for s in sessions:
        status = get_session_status(s)
        elapsed = elapsed_str(s.get('started_at', ''))
        name = s.get('_name', '?')
        task = s.get('task', '?')
        title = ensure_title(s['_dir'], s, model=llm_model)
        pid = s.get('pid', '?')
        group = s.get('group', '')

        print(f"{name}  [{title}]")
        print(f"   Task: {t(task, 100)}")
        status_line = f"   Status: {status} | PID: {pid} | Elapsed: {elapsed}"
        if group:
            status_line += f" | Group: {group}"
        print(status_line)

        if status == 'cancelled':
            print()
            continue

        last_tool = get_last_tool_activity(s['_dir'])
        if last_tool:
            print(f"   Last tool: {t(last_tool, 120)}")

        last_text = get_last_assistant_text(s['_dir'])
        if last_text:
            print(f"   Last response: {t(last_text, 200)}")

        print()

# ---------------------------------------------------------------------------
# Dump session events
# ---------------------------------------------------------------------------

def find_session_jsonl(session_dir):
    """Find pi's session JSONL file (not events.jsonl or meta.json)."""
    session_dir = Path(session_dir)
    for f in sorted(session_dir.glob("*.jsonl")):
        if f.name == "events.jsonl":
            continue
        return f
    return None

def dump_session(pattern):
    """Dump pi's session JSONL for the matching session."""
    sessions = get_all_sessions()
    matched = [s for s in sessions if pattern in s.get('_name', '') or pattern in s.get('task', '')]

    if not matched:
        print(f"No sessions matching '{pattern}'.", file=sys.stderr)
        sys.exit(1)

    if len(matched) > 1:
        print(f"Multiple sessions match '{pattern}':", file=sys.stderr)
        for s in matched:
            print(f"  {s['_name']}", file=sys.stderr)
        sys.exit(1)

    session_file = find_session_jsonl(matched[0]['_dir'])
    if not session_file:
        print(f"No session file in {matched[0]['_name']}.", file=sys.stderr)
        sys.exit(1)

    with open(session_file) as f:
        for line in f:
            line = line.strip()
            if line:
                print(line)

# ---------------------------------------------------------------------------
# Stop a session
# ---------------------------------------------------------------------------

def stop_session(pattern):
    """Stop a running session matching the pattern."""
    sessions = get_all_sessions()
    matched = [s for s in sessions if pattern in s.get('_name', '') or pattern in s.get('task', '')]

    if not matched:
        print(f"No sessions matching '{pattern}'.", file=sys.stderr)
        sys.exit(1)

    if len(matched) > 1:
        print(f"Multiple sessions match '{pattern}':", file=sys.stderr)
        for s in matched:
            print(f"  {s['_name']}", file=sys.stderr)
        sys.exit(1)

    s = matched[0]
    status = get_session_status(s)
    name = s.get('_name', '?')

    if status not in ('running', 'starting'):
        print(f"{name}: not running ({status})")
        return

    cancel_session_processes(s, s['_dir'])
    print(f"{name}: stopped")

# ---------------------------------------------------------------------------
# Append to a session
# ---------------------------------------------------------------------------

def append_session(pattern, instruction):
    """Resume an existing session with a new instruction."""
    sessions = get_all_sessions()
    matched = [s for s in sessions if pattern in s.get('_name', '') or pattern in s.get('task', '')]

    if not matched:
        print(f"No sessions matching '{pattern}'.", file=sys.stderr)
        sys.exit(1)

    if len(matched) > 1:
        print(f"Multiple sessions match '{pattern}':", file=sys.stderr)
        for s in matched:
            print(f"  {s['_name']}", file=sys.stderr)
        sys.exit(1)

    s = matched[0]
    name = s.get('_name', '?')
    status = get_session_status(s)
    session_dir = s['_dir']

    if status in ('running', 'starting'):
        print(f"{name}: still running, stop it first", file=sys.stderr)
        sys.exit(1)

    # Set the prompt and resuming flag for the worker to pick up
    meta = load_meta(session_dir)
    meta['prompt'] = instruction
    meta['resuming'] = True
    meta['status'] = 'starting'
    meta['finished_at'] = None
    meta['exit_code'] = None
    save_meta(session_dir, meta)

    worker_cmd = ["uv", "run", __file__, "--worker", str(session_dir)]
    proc = subprocess.Popen(
        worker_cmd,
        stdin=subprocess.DEVNULL,
        stdout=open(Path(session_dir) / "worker_stdout.log", 'a'),
        stderr=open(Path(session_dir) / "worker_stderr.log", 'a'),
        start_new_session=True,
    )

    meta['worker_pid'] = proc.pid
    save_meta(session_dir, meta)

    print(f"{name}: appended, resuming")

# ---------------------------------------------------------------------------
# Main instruction handler
# ---------------------------------------------------------------------------

BOSS_SYSTEM_PROMPT = """\
You are pi-boss, a dispatcher that manages coding agent instances.

You receive user instructions and decide what to do. You have two actions:

1. START a new task — spin up a new pi coding agent to do work.
2. REPORT on existing work — summarize status of running/completed tasks.

Current sessions:
{sessions_context}

Respond with a JSON object (no markdown fencing):

For starting a new task:
{{
  "action": "start",
  "slug": "short-kebab-case-slug",
  "prompt": "the detailed instruction to give to the pi coding agent",
  "response": "brief acknowledgment to the user about what you're starting"
}}

For reporting on existing work:
{{
  "action": "report",
  "response": "your summary/answer about the existing work"
}}

Rules:
- The slug should be 2-4 words, descriptive, kebab-case, max 30 chars.
- The prompt should be a clear, complete instruction for the coding agent. Expand on the user's request if needed — the agent has full access to read, write, edit files and run bash commands.
- If the user asks about status/progress of existing work, use the session info to answer.
- If ambiguous, prefer starting a new task.
- Always include a brief, friendly "response" for the user.
- The "response" field will be spoken aloud via text-to-speech. Write it as natural, conversational speech. No bullet points, numbered lists, markdown, bold, italic, backticks, code fences, or other visual formatting. Use plain sentences and short paragraphs. Paths and filenames can be written normally. Prefer brevity — one to three sentences is ideal.
"""

def handle_instruction(instruction, group=None, debounce_secs=DEFAULT_DEBOUNCE_SECS, cwd=None):
    """Use the boss brain to interpret an instruction and act on it.

    Acquires the session lock, cancels stale/superseded sessions, then
    routes through the boss LLM.
    """
    with SessionLock():
        # Cancel young sessions and group-matched sessions before doing anything
        cancelled = cancel_young_sessions(debounce_secs, group=group)
        if cancelled:
            print(f"⏭  Superseded: {', '.join(cancelled)}", file=sys.stderr)

        sessions_context = build_sessions_context()
        system = BOSS_SYSTEM_PROMPT.format(sessions_context=sessions_context)

        try:
            raw = call_boss_llm(system, instruction)
        except Exception as e:
            print(f"Error calling boss LLM: {e}", file=sys.stderr)
            sys.exit(1)

        # Parse JSON response
        try:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                decision = json.loads(json_match.group())
            else:
                print(f"Boss LLM returned non-JSON:\n{raw}", file=sys.stderr)
                sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Failed to parse boss response:\n{raw}\n\nError: {e}", file=sys.stderr)
            sys.exit(1)

        action = decision.get('action', '')
        response = decision.get('response', '')

        if action == 'start':
            slug = slugify(decision.get('slug', 'task'))
            prompt = decision.get('prompt', instruction)
            session_name = start_task(slug, prompt, group=group, cwd=cwd)
            print(response)
            print(f"\n📂 Session: {session_name}")

        elif action == 'report':
            print(response)

        else:
            print(f"Unknown action '{action}' from boss LLM. Raw:\n{raw}", file=sys.stderr)
            sys.exit(1)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv):
    """Parse CLI arguments. Returns (command, opts, rest)."""
    opts = {
        'group': None,
        'debounce': DEFAULT_DEBOUNCE_SECS,
        'json': False,
        'full': False,
        'limit': None,
        'llm': None,
        'cwd': None,
    }
    rest = []
    i = 0
    command = None

    while i < len(argv):
        arg = argv[i]

        if arg == '--worker' and i + 1 < len(argv):
            return ('worker', opts, [argv[i + 1]])
        elif arg == '--group' and i + 1 < len(argv):
            opts['group'] = argv[i + 1]
            i += 2
            continue
        elif arg == '--debounce' and i + 1 < len(argv):
            opts['debounce'] = float(argv[i + 1])
            i += 2
            continue
        elif arg == '--json':
            opts['json'] = True
            i += 1
            continue
        elif arg == '--full':
            opts['full'] = True
            i += 1
            continue
        elif arg == '--limit' and i + 1 < len(argv):
            opts['limit'] = int(argv[i + 1])
            i += 2
            continue
        elif arg == '--llm' and i + 1 < len(argv):
            opts['llm'] = argv[i + 1]
            i += 2
            continue
        elif arg == '--cwd' and i + 1 < len(argv):
            opts['cwd'] = argv[i + 1]
            i += 2
            continue
        elif arg in ('status', 'dump', 'stop', 'append', 'help') and command is None and not rest:
            command = arg
        elif arg in ('--help', '-h') and command is None and not rest:
            command = 'help'
        else:
            rest.append(arg)
        i += 1

    return (command or 'instruction', opts, rest)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    command, opts, rest = parse_args(sys.argv[1:])

    if command == 'worker':
        run_worker(rest[0])
        return

    if command == 'status':
        pattern = rest[0] if rest else None
        print_status(pattern, as_json=opts['json'], full=opts['full'], llm_model=opts['llm'], limit=opts['limit'])
        return

    if command == 'dump':
        if not rest:
            print("Usage: pi-boss dump <pattern>", file=sys.stderr)
            sys.exit(1)
        dump_session(rest[0])
        return

    if command == 'stop':
        if not rest:
            print("Usage: pi-boss stop <pattern>", file=sys.stderr)
            sys.exit(1)
        stop_session(rest[0])
        return

    if command == 'append':
        if len(rest) < 2:
            print("Usage: pi-boss append <pattern> \"instructions\"", file=sys.stderr)
            sys.exit(1)
        append_session(rest[0], ' '.join(rest[1:]))
        return

    if command == 'help':
        print(__doc__.strip())
        return

    # instruction mode
    instruction = ' '.join(rest) if rest else None
    if not instruction and not sys.stdin.isatty():
        instruction = sys.stdin.read().strip()

    if not instruction:
        print(__doc__.strip())
        sys.exit(1)

    handle_instruction(instruction, group=opts['group'], debounce_secs=opts['debounce'], cwd=opts['cwd'])

if __name__ == '__main__':
    main()
