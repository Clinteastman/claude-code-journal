#!/usr/bin/env python3
"""
journal.py - Stop hook that appends a one-line entry to a daily journal
file in the project root, so you can grep what was done across past
sessions.

Auto-runs after every assistant turn when the claude-code-journal plugin
is enabled. Per-project behaviour is controlled by an optional config
file at <project>/.claude/journal.json:

  {
    "per_user": true,         // write to journal/<gh-username>/<date>.md
                              //  (omit or false: journal/<date>.md)
    "llm": false              // optional: experimental LLM enrichment via
                              //  background `claude -p` subprocess. Off by
                              //  default - the deterministic line is
                              //  reliable, the LLM path can leave orphan
                              //  subprocesses on Windows.
  }

The hook is fire-and-forget (<100ms) and never blocks the session.
Errors are silently logged to <project>/journal/.errors.log.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_USER_CHARS = 100
HAIKU_MODEL = "claude-haiku-4-5"


def project_root():
    """Resolve the project root. Prefer CLAUDE_PROJECT_DIR (set by Claude
    Code for hooks), fall back to cwd."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


PROJECT_ROOT = project_root()
ERROR_LOG = PROJECT_ROOT / "journal" / ".errors.log"


def log_error(msg):
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def load_config():
    cfg_file = PROJECT_ROOT / ".claude" / "journal.json"
    if not cfg_file.exists():
        return {}
    try:
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception as e:
        log_error(f"load_config: {e}")
        return {}


def detect_user():
    """Resolve a stable per-user identifier so multi-machine same-user
    journals end up in the same folder. Tries gh CLI -> git config -> OS."""
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    try:
        r = subprocess.run(["git", "config", "user.email"],
                           capture_output=True, text=True, timeout=5,
                           cwd=str(PROJECT_ROOT))
        email = r.stdout.strip()
        if "@" in email:
            return email.split("@")[0]
    except Exception:
        pass
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def journal_dir(cfg):
    base = PROJECT_ROOT / "journal"
    # env var still honoured for backwards-compat / one-off override
    per_user = cfg.get("per_user") or os.environ.get("JOURNAL_PER_USER") == "1"
    if per_user:
        base = base / detect_user()
    return base


def read_transcript(path):
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log_error(f"read_transcript {path}: {e}")
    return entries


def extract_turn(entries):
    """Walk transcript backwards to find: last user message, the assistant's
    tool calls in this turn, and the assistant's last text reply."""
    last_user = ""
    tool_descs = []
    last_text = ""

    for entry in reversed(entries):
        role = entry.get("role") or entry.get("type")
        content = entry.get("content") or entry.get("message", {}).get("content")
        if not content:
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        if role == "user" and not last_user:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    last_user = block.get("text", "")
                    break
            if last_user:
                break

        if role in ("assistant", "model") or entry.get("message", {}).get("role") == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and not last_text:
                    last_text = block.get("text", "")
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {}) or {}
                    desc = inp.get("description") or inp.get("file_path") or inp.get("command") or inp.get("query") or ""
                    desc = str(desc).strip().splitlines()[0][:80] if desc else ""
                    tool_descs.append(f"{name}({desc})" if desc else name)

    return last_user.strip(), tool_descs, last_text.strip()


def deterministic_summary(user, tools, text):
    user_short = user[:MAX_USER_CHARS].replace("\n", " ")
    if len(user) > MAX_USER_CHARS:
        user_short += "..."
    if tools:
        tool_summary = ", ".join(tools[:5])
        if len(tools) > 5:
            tool_summary += f" +{len(tools)-5} more"
        return f"USER {user_short!r} | DID {tool_summary}"
    elif text:
        text_short = text[:MAX_USER_CHARS].replace("\n", " ")
        if len(text) > MAX_USER_CHARS:
            text_short += "..."
        return f"USER {user_short!r} | SAID {text_short!r}"
    return f"USER {user_short!r}"


def write_entry(cfg, line, prefix="| "):
    today = dt.date.today().isoformat()
    jdir = journal_dir(cfg)
    jdir.mkdir(parents=True, exist_ok=True)
    fp = jdir / f"{today}.md"
    new_file = not fp.exists()
    with fp.open("a", encoding="utf-8") as f:
        if new_file:
            f.write(f"# Journal {today}\n\n")
        timestamp = dt.datetime.now().strftime("%H:%M")
        f.write(f"- {timestamp} {prefix}{line}\n")


SYSTEM_PROMPT = (
    "You write a one-line journal entry summarizing what just happened in a coding "
    "assistant turn. Output exactly one sentence under 100 characters. Be concrete: "
    "mention specific files, hosts, services, decisions, or counts. Do not preface "
    "with 'The user' or 'The assistant' - just state the action. No emojis."
)


def build_snippet(user, tools, text):
    snippet = f"USER MESSAGE:\n{user[:500]}\n\nASSISTANT TOOL CALLS:\n"
    snippet += "\n".join(f"- {t}" for t in tools[:15]) if tools else "(none)"
    snippet += f"\n\nASSISTANT FINAL REPLY:\n{text[:500]}"
    return snippet


def spawn_summariser(cfg, snippet):
    """OPTIONAL: Fire-and-forget background process running `claude -p` for
    a Haiku summary. Off by default - set llm:true in journal.json to enable.
    Known-flaky on Windows (orphan subprocesses). Use at your own risk."""
    if not shutil.which("claude"):
        return
    blocked = {"ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION"}
    env = {k: v for k, v in os.environ.items() if k not in blocked}
    env["JOURNAL_SKIP"] = "1"

    try:
        jdir = journal_dir(cfg)
        jdir.mkdir(parents=True, exist_ok=True)
        snippet_file = jdir / f".pending-{os.getpid()}-{int(dt.datetime.now().timestamp())}.txt"
        snippet_file.write_text(snippet, encoding="utf-8")

        creation_flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--summarise", str(snippet_file)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creation_flags,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )
    except Exception as e:
        log_error(f"spawn_summariser: {e}")


def run_summariser():
    """Background mode: read snippet from CLI arg, call claude -p, append result."""
    snippet_file = None
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--summarise":
            snippet_file = Path(sys.argv[2])
            snippet = snippet_file.read_text(encoding="utf-8")
        else:
            snippet = sys.stdin.read()
        if not snippet.strip():
            return
        prompt = SYSTEM_PROMPT + "\n\n---\n" + snippet + "\n---\n\nSummary:"
        blocked = {"ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION"}
        env = {k: v for k, v in os.environ.items() if k not in blocked}
        env["JOURNAL_SKIP"] = "1"
        result = subprocess.run(
            ["claude", "-p", "--model", HAIKU_MODEL, prompt],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            log_error(f"claude -p exit {result.returncode}: {result.stderr.strip()[:200]}")
            return
        line = result.stdout.strip().splitlines()[0][:200] if result.stdout else ""
        if line:
            write_entry(load_config(), line, prefix="* ")
    except subprocess.TimeoutExpired:
        log_error("claude -p timed out after 60s")
    except Exception as e:
        log_error(f"run_summariser: {e}")
    finally:
        if snippet_file and snippet_file.exists():
            try:
                snippet_file.unlink()
            except OSError:
                pass


def main():
    if os.environ.get("JOURNAL_SKIP") == "1":
        sys.exit(0)
    if "--summarise" in sys.argv:
        run_summariser()
        sys.exit(0)

    try:
        raw = sys.stdin.read()
        if not raw:
            sys.exit(0)
        payload = json.loads(raw)
    except Exception as e:
        log_error(f"stdin parse: {e}")
        sys.exit(0)

    transcript_path = payload.get("transcript_path") or payload.get("transcript")
    if not transcript_path:
        log_error("no transcript_path in hook payload")
        sys.exit(0)

    entries = read_transcript(transcript_path)
    if not entries:
        sys.exit(0)

    user, tools, text = extract_turn(entries)
    if not user and not tools and not text:
        sys.exit(0)

    cfg = load_config()

    try:
        write_entry(cfg, deterministic_summary(user, tools, text), prefix="| ")
    except Exception as e:
        log_error(f"write_entry: {e}")

    if cfg.get("llm") is True:
        spawn_summariser(cfg, build_snippet(user, tools, text))

    sys.exit(0)


if __name__ == "__main__":
    main()
