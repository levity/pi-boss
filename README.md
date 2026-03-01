# pi-boss

A stateless CLI that governs multiple [pi](https://github.com/badlogic/pi-mono) coding agent instances via RPC mode.

## Usage

```bash
# Start new work — instruction via argument or stdin
pi-boss "build a web scraper for Hacker News"
echo "refactor the auth module to use JWT" | pi-boss

# Check on everything
pi-boss status

# Filter status to matching sessions
pi-boss status scraper

# Kill a running session
pi-boss kill scraper
```

## How it works

1. **You give an instruction** — pi-boss calls a lightweight LLM (Sonnet via the local gateway) to classify it: "start new work" or "report on existing work."
2. **New work** → pi-boss spawns a `pi --mode rpc` process in the background with its own `--session-dir`. The agent runs autonomously to completion.
3. **Status query** → pi-boss reads session metadata and event logs, feeds them to the LLM, returns a summary.
4. **`pi-boss status`** → scans all session directories, checks PIDs, tails event logs, prints a dashboard.

## Session layout

Sessions live in `./sessions/<YYYYMMDD-HHMMSS-slug>/`:

```
sessions/
└── 20260301-053524-hello-world-script/
    ├── meta.json           # task, PID, timestamps, status
    ├── events.jsonl        # captured RPC event stream
    ├── stderr.log          # pi's stderr
    ├── worker_stdout.log   # worker process stdout
    ├── worker_stderr.log   # worker process stderr
    └── sessions/           # pi's own session storage
```

## Design decisions

- **Stateless CLI**: No daemon. State is discovered by scanning `sessions/` each invocation.
- **Boss brain uses Sonnet**: Fast, cheap LLM calls via the local Anthropic gateway for routing decisions. Worker pi instances use the default model (currently Opus 4.6).
- **Fire-and-forget tasks**: Each pi instance gets a prompt and runs to completion. No mid-stream steering for v1.
- **Status from files**: `meta.json` + `events.jsonl` are the source of truth. PID liveness checks distinguish running from dead.
- **No extensions/skills/themes in workers**: Workers run with `--no-extensions --no-skills --no-prompt-templates --no-themes` to avoid interference. This can be relaxed later.
- **Extension UI auto-cancel**: If pi hits an extension UI dialog (shouldn't happen with no extensions), the worker auto-cancels it to avoid blocking.
- **Detached workers**: Worker processes use `start_new_session=True` so they survive the parent CLI exiting.

## Future

- Conversational voice interface (this becomes the backend)
- Mid-stream steering / follow-up messages to running tasks
- Session resume / re-attach
- Cost tracking (from session stats)
- Configurable model for workers
