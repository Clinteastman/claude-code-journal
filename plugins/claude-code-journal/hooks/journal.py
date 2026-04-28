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
import re
import shutil
import subprocess
import sys
from pathlib import Path

MAX_USER_CHARS = 400
MAX_TEXT_CHARS = 400
MAX_TOOLS_LISTED = 12
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


def normalise_transcript_path(path):
    """Some Claude Code setups on Windows pass Git-Bash style paths
    (e.g. /c/Users/...) which Python's open() cannot resolve. Convert
    those to a Win32 drive path so the hook works regardless of which
    shell delivered the payload."""
    if os.name != "nt" or not path:
        return path
    if len(path) >= 3 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        return path[1].upper() + ":/" + path[3:]
    return path


def read_transcript(path):
    path = normalise_transcript_path(path)
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


def _tail(text, n):
    """Return the last n chars of a single-line string, with an ellipsis prefix
    if truncated. Used for file paths so the filename stays visible."""
    text = str(text).strip().splitlines()[0] if text else ""
    if len(text) <= n:
        return text
    return "..." + text[-(n - 3):]


def _describe_tool(name, inp):
    """Render one tool_use block into a short, scannable label. Prefer the
    most identifying field per tool: file path tail for editors, description
    or first line of command for Bash, query for searches."""
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return f"{name}({_tail(inp.get('file_path', ''), 60)})"
    if name == "Bash":
        desc = inp.get("description") or ""
        if desc:
            return f"Bash({str(desc).strip().splitlines()[0][:80]})"
        cmd = (inp.get("command") or "").strip().splitlines()[0]
        return f"Bash({cmd[:80]})" if cmd else "Bash"
    if name in ("Grep", "Glob"):
        return f"{name}({(inp.get('pattern') or '')[:60]})"
    if name == "Task" or name == "Agent":
        return f"{name}({(inp.get('description') or inp.get('subagent_type') or '')[:60]})"
    desc = inp.get("description") or inp.get("query") or inp.get("command") or ""
    desc = str(desc).strip().splitlines()[0][:80] if desc else ""
    return f"{name}({desc})" if desc else name


def extract_turn(entries):
    """Walk transcript backwards to find: last user message, the assistant's
    tool calls in this turn, the assistant's last text reply, and the raw
    tool_use blocks (for heuristic synthesis)."""
    last_user = ""
    tool_descs = []
    raw_tools = []
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
                    tool_descs.append(_describe_tool(name, inp))
                    raw_tools.append({"name": name, "input": inp})

    return last_user.strip(), tool_descs, last_text.strip(), raw_tools


_GIT_COMMIT_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+commit\b", re.IGNORECASE)
_GIT_PUSH_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+push\b", re.IGNORECASE)
_GIT_MSG_RE = re.compile(r"-m\s+\"(?:\$\(cat\s*<<\s*'?EOF'?\s*\n)?(.*?)(?:\n|$|\")", re.DOTALL)


def heuristic_summary(raw_tools):
    """Synthesise a one-line summary of *what* happened in the turn from the
    raw tool blocks. Detects: git commits/pushes (with first line of message
    where possible), file edits (deduped by basename), and tools-run counts.
    Returns None if nothing notable was found, in which case the caller
    falls back to the raw tool list."""
    if not raw_tools:
        return None
    edits = []
    reads = 0
    bashes = 0
    git_actions = []  # ("committed <hash> <msg>", "pushed", ...)
    scripts = []  # python tools/foo.py invocations

    for t in raw_tools:
        name = t.get("name", "")
        inp = t.get("input") or {}
        if name in ("Edit", "Write", "NotebookEdit"):
            fp = inp.get("file_path", "")
            if fp:
                edits.append(os.path.basename(fp))
        elif name == "Read":
            reads += 1
        elif name == "Bash":
            bashes += 1
            cmd = inp.get("command") or ""
            if _GIT_PUSH_RE.search(cmd):
                git_actions.append("pushed")
            if _GIT_COMMIT_RE.search(cmd):
                msg_match = _GIT_MSG_RE.search(cmd)
                first_line = ""
                if msg_match:
                    first_line = msg_match.group(1).strip().splitlines()[0][:60]
                git_actions.append(f"committed '{first_line}'" if first_line else "committed")
            m = re.search(r"python3?\s+(\S*tools/[\w\-]+\.py)", cmd)
            if m:
                scripts.append(os.path.basename(m.group(1)))

    parts = []
    # dedupe git_actions while preserving order
    seen = set()
    git_dedup = [a for a in git_actions if not (a in seen or seen.add(a))]
    if git_dedup:
        parts.append("; ".join(git_dedup))
    if edits:
        unique = list(dict.fromkeys(edits))
        if len(unique) <= 3:
            parts.append("edited " + ", ".join(unique))
        else:
            parts.append(f"edited {', '.join(unique[:2])} +{len(unique)-2}")
    if scripts:
        unique_scripts = list(dict.fromkeys(scripts))
        parts.append("ran " + ", ".join(unique_scripts[:3]))
    return "; ".join(parts) if parts else None


def _truncate(s, n):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def deterministic_summary(user, tools, text, raw_tools=None):
    user_short = _truncate(user, MAX_USER_CHARS)
    synth = heuristic_summary(raw_tools or [])
    if synth:
        return f"USER {user_short!r} | DID {synth}"
    if tools:
        tool_summary = ", ".join(tools[:MAX_TOOLS_LISTED])
        if len(tools) > MAX_TOOLS_LISTED:
            tool_summary += f" +{len(tools)-MAX_TOOLS_LISTED} more"
        return f"USER {user_short!r} | DID {tool_summary}"
    if text:
        return f"USER {user_short!r} | SAID {_truncate(text, MAX_TEXT_CHARS)!r}"
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


def _spawn_detached(cmd, env):
    """Launch a fully detached subprocess. On Windows the Stop hook runs
    inside a Job Object that the parent harness terminates when it exits;
    without CREATE_BREAKAWAY_FROM_JOB the child gets reaped before
    `claude -p` can finish, which is why earlier versions appeared 'flaky'.
    Falls back gracefully if the Job forbids breakaway."""
    common = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        close_fds=True,
    )
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        try:
            return subprocess.Popen(cmd, creationflags=flags, **common)
        except OSError:
            flags &= ~CREATE_BREAKAWAY_FROM_JOB
            return subprocess.Popen(cmd, creationflags=flags, **common)
    return subprocess.Popen(cmd, start_new_session=True, **common)


def spawn_summariser(cfg, snippet):
    """OPTIONAL: Fire-and-forget background process running `claude -p` for
    a Haiku summary. Off by default - set llm:true in journal.json to enable."""
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

        _spawn_detached(
            [sys.executable, str(Path(__file__).resolve()), "--summarise", str(snippet_file)],
            env,
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

    user, tools, text, raw_tools = extract_turn(entries)
    if not user and not tools and not text:
        sys.exit(0)

    cfg = load_config()

    try:
        write_entry(cfg, deterministic_summary(user, tools, text, raw_tools), prefix="| ")
    except Exception as e:
        log_error(f"write_entry: {e}")

    if cfg.get("llm") is True:
        spawn_summariser(cfg, build_snippet(user, tools, text))

    sys.exit(0)


if __name__ == "__main__":
    main()
