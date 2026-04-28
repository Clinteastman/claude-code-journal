#!/usr/bin/env python3
"""
journal.py - Stop hook that appends a one-line entry to a daily journal
file in the project root, so you can grep what was done across past
sessions.

Auto-runs after every assistant turn when the claude-code-journal plugin
is enabled. Per-project behaviour is controlled by an optional config
file at <project>/.claude/journal.json:

  {
    "per_user": true          // write to journal/<gh-username>/<date>.md
                              //  (omit or false: journal/<date>.md)
  }

The hook is fire-and-forget (<100ms) and never blocks the session.
Errors are silently logged to <project>/journal/.errors.log.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_USER_CHARS = 400
MAX_TEXT_CHARS = 400
MAX_TOOLS_LISTED = 12


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


_COMMIT_HASH_RE = re.compile(r"\[(?:[\w/.-]+\s+)+([a-f0-9]{7,40})\]")


def _flatten_tool_result(content):
    """tool_result.content is sometimes a string, sometimes a list of blocks.
    Return a single string we can grep."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or "")
        return "\n".join(parts)
    return ""


def extract_turn(entries):
    """Walk transcript backwards to find: last user message, the assistant's
    tool calls in this turn, the assistant's last text reply, the raw
    tool_use blocks (for heuristic synthesis), and any commit hashes that
    appeared in tool_result output (so the journal can show `[abc1234]`
    next to a committed entry)."""
    last_user = ""
    tool_descs = []
    raw_tools = []
    last_text = ""
    commit_hashes = []

    for entry in reversed(entries):
        role = entry.get("role") or entry.get("type")
        content = entry.get("content") or entry.get("message", {}).get("content")
        if not content:
            continue
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        if role == "user":
            # tool_result blocks live in user-role entries and may contain
            # the stdout of a prior `git commit` - mine them for the hash
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _flatten_tool_result(block.get("content"))
                    for m in _COMMIT_HASH_RE.finditer(text):
                        commit_hashes.append(m.group(1)[:7])
            # then check if this is the user's own text prompt that started
            # the turn (which terminates the walk)
            if not last_user:
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

    # Walk was reverse-chronological, so commit_hashes are newest-first.
    # Reverse to put them in the order the turn actually executed them.
    commit_hashes.reverse()
    return last_user.strip(), tool_descs, last_text.strip(), raw_tools, commit_hashes


_GIT_COMMIT_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+commit\b", re.IGNORECASE)
_GIT_PUSH_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+push\b", re.IGNORECASE)
_GIT_MSG_RE = re.compile(r"-m\s+\"(?:\$\(cat\s*<<\s*'?EOF'?\s*\n)?(.*?)(?:\n|$|\")", re.DOTALL)
_MEMORY_PATH_RE = re.compile(
    r"[\\/]\.claude[\\/]projects[\\/][^\\/]+[\\/]memory[\\/]([^\\/]+\.md)$|[\\/]MEMORY\.md$",
    re.IGNORECASE,
)


def _classify_path(fp):
    """Return ('memory', filename) for auto-memory file paths, otherwise
    ('edit', basename). Lets the synthesiser surface decision-records
    distinctly from regular file edits."""
    if not fp:
        return ("edit", "")
    m = _MEMORY_PATH_RE.search(fp)
    if m:
        return ("memory", os.path.basename(fp))
    return ("edit", os.path.basename(fp))


def heuristic_summary(raw_tools, commit_hashes=None):
    """Synthesise a one-line summary of *what* happened in the turn from the
    raw tool blocks. Detects: git commits/pushes (with first line of message
    and short hash where available), memory saves, file edits (deduped by
    basename), and tools/* scripts run. Returns None if nothing notable was
    found, in which case the caller falls back to the raw tool list."""
    if not raw_tools:
        return None
    edits = []
    memories = []
    git_actions = []
    scripts = []
    commit_idx = 0
    hashes = list(commit_hashes or [])

    for t in raw_tools:
        name = t.get("name", "")
        inp = t.get("input") or {}
        if name in ("Edit", "Write", "NotebookEdit"):
            kind, fname = _classify_path(inp.get("file_path", ""))
            if kind == "memory" and fname:
                memories.append(fname)
            elif fname:
                edits.append(fname)
        elif name == "Bash":
            cmd = inp.get("command") or ""
            if _GIT_PUSH_RE.search(cmd):
                git_actions.append("pushed")
            if _GIT_COMMIT_RE.search(cmd):
                msg_match = _GIT_MSG_RE.search(cmd)
                first_line = ""
                if msg_match:
                    first_line = msg_match.group(1).strip().splitlines()[0][:60]
                # Pair this commit with the next available hash from tool output
                short = ""
                if commit_idx < len(hashes):
                    short = f" [{hashes[commit_idx]}]"
                    commit_idx += 1
                base = f"committed '{first_line}'" if first_line else "committed"
                git_actions.append(base + short)
            m = re.search(r"python3?\s+(\S*tools/[\w\-]+\.py)", cmd)
            if m:
                scripts.append(os.path.basename(m.group(1)))

    def _join(label, items, max_listed=3):
        unique = list(dict.fromkeys(items))
        if not unique:
            return None
        if len(unique) <= max_listed:
            return f"{label} " + ", ".join(unique)
        return f"{label} {', '.join(unique[:2])} +{len(unique)-2}"

    parts = []
    seen = set()
    git_dedup = [a for a in git_actions if not (a in seen or seen.add(a))]
    if git_dedup:
        parts.append("; ".join(git_dedup))
    if memories:
        parts.append(_join("saved memory", memories))
    if edits:
        parts.append(_join("edited", edits))
    if scripts:
        parts.append(_join("ran", scripts))
    return "; ".join(p for p in parts if p) or None


def _truncate(s, n):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


# When a turn includes both tool calls and an assistant reply, include both
# but cap the reply tighter to keep the line scannable. Pure-text turns
# still get the full MAX_TEXT_CHARS budget.
SAID_WITH_TOOLS_CHARS = 220


def deterministic_summary(user, tools, text, raw_tools=None, commit_hashes=None):
    user_short = _truncate(user, MAX_USER_CHARS)
    synth = heuristic_summary(raw_tools or [], commit_hashes)
    if tools or synth:
        if synth:
            did = synth
        else:
            did = ", ".join(tools[:MAX_TOOLS_LISTED])
            if len(tools) > MAX_TOOLS_LISTED:
                did += f" +{len(tools)-MAX_TOOLS_LISTED} more"
        line = f"USER {user_short!r} | DID {did}"
        if text:
            line += f" | SAID {_truncate(text, SAID_WITH_TOOLS_CHARS)!r}"
        return line
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


def main():
    if os.environ.get("JOURNAL_SKIP") == "1":
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

    user, tools, text, raw_tools, commit_hashes = extract_turn(entries)
    if not user and not tools and not text:
        sys.exit(0)

    cfg = load_config()

    try:
        write_entry(
            cfg,
            deterministic_summary(user, tools, text, raw_tools, commit_hashes),
            prefix="| ",
        )
    except Exception as e:
        log_error(f"write_entry: {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
