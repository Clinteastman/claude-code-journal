# claude-code-journal

> A [Claude Code](https://code.claude.com) plugin that records every assistant turn as one greppable line of markdown, so you can recall what you did yesterday without trawling chat history.

## What you get

After every assistant turn, a Stop hook appends one line to a daily file in your project:

```
- 14:23 | USER 'fix the broken header on the lean-to page' | DID Bash(fetch header), Edit(rewrite copy), Bash(push to test)
- 14:31 | USER 'looks good, push to prod' | DID Bash(re-push with --production)
- 14:55 | USER 'check sales for last 30 days' | DID Bash(query orders), Bash(format table)
```

Plain markdown. No JSON to parse. No per-session directories. No cloud dependency. `cat`, `grep`, `tail -f` - all the old tools work.

## Why

When you're working on a long-running project with Claude Code, conversations span days or weeks across many sessions. The chat history is volatile and hard to grep. Without an artifact you can search later, you and Claude both end up jumping the gun on the same diagnoses repeatedly.

This plugin gives you a permanent, attributable record of what was tried, in plain markdown that lives next to your code.

## Install

### From inside Claude Code (easiest)

Run these two slash commands in any Claude Code session:

```
/plugin marketplace add Clinteastman/claude-code-journal
/plugin install claude-code-journal@claude-code-journal
```

### From your terminal

```bash
claude plugin marketplace add Clinteastman/claude-code-journal
claude plugin install claude-code-journal@claude-code-journal
```

Either way: install runs once per machine + per user. After that, the hook fires automatically in every Claude Code session you start, in every project, until you uninstall.

To update later: `claude plugin update claude-code-journal` (or via `/plugin update`).

## Use it

Just work normally. The hook captures every turn. To read entries back:

```
/journal              # today's entries
/journal yesterday    # yesterday
/journal 2026-04-27   # specific date
/journal user matt    # someone else's journal (per-user mode only)
```

Or just open the file directly: `journal/2026-04-27.md` (or `journal/<your-gh-username>/2026-04-27.md` in per-user mode).

## How it works across machines and team members

The plugin writes the journal **into the project directory**. Combined with normal git workflow, that gives you some genuinely useful properties for free:

### Solo + multi-machine

You work on the same project from a desktop AND a laptop. Same git repo on both.

1. Desktop session: hook writes a few lines to `journal/2026-04-27.md`
2. You commit + push (alongside whatever code work prompted those entries)
3. Switch to laptop, `git pull`
4. Laptop session: hook reads the same file, appends new lines
5. Commit + push from laptop
6. Back at desktop, `git pull` brings the laptop's additions down

Result: one continuous timeline per day, attributed to you, persisted in git, surviving any machine wipe. No third-party sync, no extra credentials, no setup beyond what you already do for the code.

### Team + per-user mode

Multiple devs working in the same repo. Add `.claude/journal.json` with `{"per_user": true}` and each teammate's entries land in their own subdirectory keyed by GitHub username:

```
journal/
├── alice/
│   └── 2026-04-27.md
├── bob/
│   └── 2026-04-27.md
└── chris/
    └── 2026-04-27.md
```

Identity is resolved via `gh api user --jq .login`, so the same person on different machines converges in the same folder. No collisions between teammates' files = no merge conflicts. You can `cat journal/bob/2026-04-27.md` to see what Bob was doing today, without having to ask him.

`git log journal/` and `git blame` work as expected on these files - so you literally get an attributed, time-stamped, version-controlled log of every action across the team.

## Configuration

Optional `.claude/journal.json` in your project root tunes per-project behaviour:

```json
{
  "per_user": true
}
```

| Key | Default | Effect |
|---|---|---|
| `per_user` | `false` | When `true`, writes to `journal/<gh-username>/<date>.md` instead of `journal/<date>.md`. Use this in shared work repos. |
| `llm` | `false` | EXPERIMENTAL: spawns a background `claude -p` subprocess for an LLM-summarised second line per turn. Uses your Claude Code subscription (no API key). Currently flaky - leaves orphan subprocesses on Windows. Don't enable unless you want to debug it. |

If neither file nor key is set: single-user mode, no LLM enrichment, journal at `journal/<date>.md`.

## Output format

Each line is one of:

```
- HH:MM | USER '<first 100 chars of prompt>' | DID Tool(desc), Tool(desc) +N more
- HH:MM | USER '<prompt>' | SAID '<first 100 chars of assistant final reply>'   (when no tools used)
- HH:MM * <LLM-summarised one-liner>                                              (only if llm:true)
```

The `|` lines are deterministic - they always land within ~100ms of the Stop event. The `*` lines (LLM mode) follow a few seconds later if the LLM call succeeds.

## What gets logged

- The user's most recent prompt (truncated to 100 chars)
- Names + descriptions of tools the assistant used in that turn
- The assistant's last text reply (only if no tools were used)

What does NOT get logged:
- Full conversation transcripts (too noisy)
- Tool outputs (too large; the description tells you what was attempted)
- Assistant reasoning blocks (private)
- Plain conversation text when tools were used (the tool call summary is the substance)

## Skipping a turn

Set `JOURNAL_SKIP=1` in the shell before a one-off prompt you don't want recorded:

```bash
JOURNAL_SKIP=1 claude -p "ad-hoc test, don't journal this"
```

## Should I commit the journal?

**Solo personal repos:** yes, commit it. You get free history backup, multi-machine sync, and `git log journal/` becomes useful.

**Team work repos with `per_user: true`:** yes, commit it. Each teammate's entries are isolated to their own subdirectory; work content is rarely a privacy issue. You get attributed history, multi-machine convergence, and easy "what did Alice do last Tuesday?" lookups.

**If you really don't want to commit it:** add `journal/` to `.gitignore`. The plugin still works locally; you just lose the cross-machine and cross-team benefits.

## Where errors go

Hook failures are silently logged to `<project>/journal/.errors.log` instead of bubbling up to the user. The hook is designed to **never block** a session - if anything goes wrong, the worst case is a missing journal line, not a broken Claude Code experience.

## Uninstall

```
/plugin uninstall claude-code-journal
```

The plugin's hook stops firing immediately. Existing journal files are left alone (they're inside your projects, not owned by the plugin).

## Contributing

Issues + PRs welcome at https://github.com/Clinteastman/claude-code-journal. The plugin is small (one Python script with no dependencies, plus a slash command) and easy to read.

## License

MIT
