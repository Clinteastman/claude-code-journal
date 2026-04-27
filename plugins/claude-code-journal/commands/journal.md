---
description: Show recent journal entries so we can recall what's already been done in past sessions.
argument-hint: "[today|yesterday|YYYY-MM-DD|user <name>]"
---

The journal lives at `journal/<date>.md` (single-user) or `journal/<gh-username>/<date>.md` (per-user mode, set by `.claude/journal.json` -> `per_user: true`). The Stop hook from the **claude-code-journal** plugin writes them automatically on every assistant turn.

Look at the requested window and read the matching journal file(s) yourself.

Argument behaviour:
- no arg or `today` -> read today's file (own entries if per-user mode)
- `yesterday` -> yesterday's file
- a date like `2026-04-27` -> that day's file
- `user <name>` -> read another teammate's journal (per-user mode only)
- `user <name> 2026-04-27` -> teammate's specific date

Resolve which path to read by checking `.claude/journal.json` for `per_user`. If it's true, the path is `journal/<gh-username>/<date>.md` - get your own gh username via `gh api user --jq .login`.

If the requested file doesn't exist, say so and list what IS available in `journal/` (or in the relevant user subdir).

When reporting back: just print the journal lines verbatim. Don't summarise or rephrase. The whole point is to surface the raw record.

Argument: $ARGUMENTS
