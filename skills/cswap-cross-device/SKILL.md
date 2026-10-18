---
name: cswap-cross-device
description: Message and steer Claude Code worker sessions that run under another cswap account or on another machine with the `cxd` CLI. Use it to start a headless worker, send it instructions mid-run, watch its turns and reports, stop or resume it, or reach an already-running interactive session. Workers use `cxd notify` to report back to the lead.
---

# cswap-cross-device (`cxd`)

`cxd` connects a lead session to worker sessions that run under a different cswap
account (for example a `work` worker started from a `personal` lead) or on another
machine (for example `laptop` and `buildbox`). It is on the Bash tool's PATH while this
plugin is enabled. All state lives in `~/.cxd/` (mode 700) on the host that owns the
run. No network listener is used: remote hosts are reached over your own ssh alias.

## Hosts

Every command names its host: a name from `~/.cxd/config.json` (or `local` for this
machine). `cxd init --self <name> [--peer <ssh-alias>]` writes that file and links
`~/.cxd/bin/cxd`, which is the path peers run over `ssh <alias>`. Run `cxd doctor` to
see which hosts are reachable.

## Lead: headless workers (stdin-controlled)

```bash
cxd start buildbox w1 prompt.md --account work --cwd ~/src/project -- --model sonnet
cxd send  buildbox w1 "Also run the test suite before you open the PR."
cxd watch buildbox w1            # run with the Monitor tool: one line per event
cxd list                         # every configured host: runs + live sessions
cxd stop  buildbox w1            # keeper EOF -> wait -> SIGINT -> SIGTERM
cxd resume buildbox w1 next.md   # only after it has exited; same session id
```

- `start` launches `cswap run <account> -- -p --input-format stream-json ...` (plain
  `claude -p ...` when no account is given and no `default_account` is configured) with a
  FIFO as stdin. A keeper process holds the FIFO open, so the session stays up between
  messages. Do not open that FIFO yourself: an extra writer keeps a stopped run alive.
- Permission prompts are not bypassed by default. Nobody can answer them in a headless
  run, so a tool call that needs approval is refused and the worker may stall; `start`
  warns once on stderr. Prefer granting what the task needs after `--`:
  `-- --permission-mode acceptEdits`, `-- --allowedTools "Bash(git *)"`.
- Skipping every check (`--dangerously-skip-permissions`) is an explicit opt-in:
  `cxd start ... --skip-permissions`, or `"skip_permissions": true` in the worker host's
  `~/.cxd/config.json`. A bypassed worker can run any command and change any file the
  user can, so use it only in a directory and on a machine you would let it change freely.
- `send` to a headless run becomes the next user turn. A message sent mid-turn is queued
  or folded into the running turn. `send` refuses a run that is not running (exit 3).
- `watch` prints: `status`, `turn N ended ... cost=$...: <result excerpt>`, `PR <url>`,
  `report (notify): ...`, `heartbeat`, and finally `EXITED rc=...`, then exits 0. A line
  `transport lost (...); run state unknown, not exited` means only the ssh channel broke:
  the run is still in whatever state it was, and `watch` reconnects by itself. Only a
  line that says `EXITED` means the process ended.
- `stop --now` skips the grace period and interrupts the current turn.
- Exit codes: 0 ok, 2 usage, 3 refused (dead run, alive on resume, unknown name),
  4 transport failure (the host could not be reached; nothing was decided).

## Lead: interactive or plain sessions (hook inbox)

Any session of an account with this plugin enabled registers itself at start
(`~/.cxd/sessions/<session_id>.json`). Address it by session id (or a unique 8+ char
prefix):

```bash
cxd list laptop                                  # shows live sessions with their ids
cxd send laptop 3f2a9c1e "Pause after the current file and report status."
cxd watch laptop 3f2a9c1e                        # delivery receipts + Stop reports
```

The message waits in `~/.cxd/inbox/<session_id>/` until that session's next hook fires
(after a tool call, on a prompt, or at the end of a turn) and is then delivered exactly
once. It arrives as context reading
`Message from the lead session <sender> at <time>: <text>`. A session that is idle
waiting for a human only sees it on its next prompt.

## Worker: reporting back

Inside a worker session, report to whoever watches you:

```bash
cxd notify "PR opened: https://github.com/org/repo/pull/123 - tests green"
```

`notify` appends to `~/.cxd/outbox/<your session id>.jsonl` (it reads
`CLAUDE_CODE_SESSION_ID`). The lead's `cxd watch` shows it as `report (notify): ...`.
The end of every turn is also recorded automatically (the Stop hook), so a plain final
answer reaches the lead too. If you receive a "Message from the lead session" context
line, treat it as the lead's instruction for the current task.

Never put secrets (tokens, keys, passwords) in a `send` or `notify` message: messages
are stored as plain files under `~/.cxd/`.

## Checks

`cxd doctor` checks every configured host: ssh round-trip, cswap and claude present,
the hook self-test (an empty inbox is silent, a queued message is delivered once), and
whether the plugin is installed and enabled for each configured account (or for the
default profile when no accounts are configured).
