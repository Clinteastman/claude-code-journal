# claude-code-journal

A [Claude Code](https://code.claude.com) plugin that auto-captures a one-line summary of every assistant turn to a daily markdown file. Lets you grep what you did last Tuesday at 3pm without trawling chat history.

## Why

When working on long-running projects with Claude Code, conversations span days or weeks across multiple sessions. Memory between sessions is volatile. Without a record of "I tried X and Y didn't work for reason Z", you and Claude both end up jumping the gun on the same diagnoses repeatedly.

This plugin runs a Stop hook after every assistant turn and writes one line of plain markdown to a daily file:

```
- 14:23 | USER 'fix the broken header on the lean-to page' | DID Bash(fetch header), Edit(rewrite copy), Bash(push to test)
- 14:31 | USER 'looks good, push to prod' | DID Bash(re-push with --production)
```

That's it. No JSON to parse, no per-session directories to dig through, no cloud dependency. Just plain markdown you can grep, cat, or open in any editor.

## Install

```bash
claude plugin install https://github.com/Clinteastman/claude-code-journal
```

That's it. The hook starts firing on the next session. No per-repo setup needed for default (single-user) behaviour.

To update later:
```bash
claude plugin update claude-code-journal
```

## Use

The `/journal` slash command reads back recent entries:

```
/journal              # today's entries
/journal yesterday    # yesterday's entries
/journal 2026-04-27   # specific date
/journal user matt    # someone else's journal (per-user mode)
```

You can also just `cat journal/2026-04-27.md` - they're plain files.

## Per-repo configuration

Optional `.claude/journal.json` in your project root tunes behaviour per project:

```json
{
  "per_user": true
}
```

| Key | Default | Effect |
|---|---|---|
| `per_user` | `false` | When `true`, writes to `journal/<gh-username>/<date>.md` instead of `journal/<date>.md`. Use this in shared work repos so each teammate's entries are separated. Username comes from `gh api user --jq .login` so multi-machine sessions for the same person converge in one folder. |
| `llm` | `false` | EXPERIMENTAL: when `true`, also spawns a background `claude -p` subprocess to write a second LLM-summarised line. Uses your Claude Code subscription (no API key). Currently flaky - leaves orphan subprocesses on Windows. Don't enable unless you want to debug it. |

If neither file nor key is set: single-user mode, no LLM enrichment, journal at `journal/<date>.md`.

## Output format

Each line is one of:

```
- HH:MM | USER '<first 100 chars of prompt>' | DID Tool(desc), Tool(desc) +N more
- HH:MM | USER '<prompt>' | SAID '<first 100 chars of assistant final reply>'   (when no tools used)
- HH:MM * <LLM-summarised one-liner>                                              (only if llm:true)
```

The `|` lines are deterministic and always land within ~100ms of the Stop event. The `*` lines (LLM mode) follow a few seconds later and may not always land if the LLM call fails.

## What gets logged

- The user's most recent prompt (truncated to 100 chars)
- Names + descriptions of tools the assistant used in that turn
- The assistant's last text reply (only if no tools were used)

What does NOT get logged:
- Full conversation transcripts (too noisy)
- Tool outputs (too large; the description tells you what was attempted)
- Assistant reasoning blocks (private)

## Skipping a turn

Set `JOURNAL_SKIP=1` in the shell before a prompt if you want that one turn to skip the journal:

```bash
JOURNAL_SKIP=1 claude -p "ad-hoc one-off, don't journal this"
```

## Storage location

The journal lives at `<project-root>/journal/...` - so it's tied to the project, not your home directory. This is deliberate:

- For solo personal repos: commit `journal/` to git, you get a free history backup
- For shared work repos with `per_user: true`: still commit it; each teammate's subdirectory is attributed by gh username, work content shouldn't be a privacy issue
- If you don't want to commit it: add `journal/` to `.gitignore`

## Where errors go

Hook failures are silently logged to `journal/.errors.log` instead of bubbling up to the user. The hook is designed to never block a session - if anything goes wrong, the worst case is a missing journal line, not a broken Claude Code experience.

## License

MIT
