# Hook-inbox delivery into an interactive Claude Code session (Linux)

- **Date:** 2026-10-18
- **What:** `cxd send` to an interactive (TUI) Claude Code session, delivered by the plugin's hooks.
- **How:** `scripts/evidence/tui_delivery.py` drives a real `claude` in a private tmux server, under a cswap account, with a throwaway `CXD_HOME`, using the Haiku model and tiny prompts.
- **Evidence:** cxd's own delivery receipts, which name the hook that delivered each message, plus Claude's replies read from the session transcript.

| Case | Setup | Result |
| --- | --- | --- |
| A | Message queued while a Bash tool call (`sleep 12`) runs | Delivered by **PostToolUse**; Claude's reply includes the marker |
| B | Message queued while idle, then a prompt is typed | Delivered by **UserPromptSubmit**; the reply includes the marker |
| C | Message queued while a tool-free answer is written | Delivered by **Stop**; the session **continued** (a second Stop run) and acted on it (follow-up: the marker) |
| D | Message queued while the session is idle, nobody typing | **Not delivered while idle** (30 s); delivered by the next prompt |

Case D is the documented limitation of hook delivery: hooks only run while the session is doing something.

A case passes only when cxd's receipt names the expected hook and Claude's reply (read from the transcript) follows the message. The log below was written by the driver as committed, with no hand edits.

Reproduce:

```bash
python3 scripts/evidence/tui_delivery.py --account <cswap-account> --plugin-dir . --out log.md
```

## Log (redacted)

```
[t+  0.1s] Claude Code 2.1.302 (as launched by cswap); cxd 0.1.0; plugin tree at 2517b660ea69; Linux x86_64; python 3.14.7; model haiku
[t+  0.2s] started claude TUI in a private tmux server: cswap run <account> -- --plugin-dir <plugin-dir> --model haiku --allowedTools Bash(sleep *)
[t+  2.7s] workspace trust dialog shown; choosing 'Yes, I trust this folder'
[t+  4.2s] SessionStart hook registered the session (pid recorded: True)
[t+  4.3s] cxd list shows the session alive=True
[t+  4.3s] === A: delivery on PostToolUse
[t+  9.8s] cxd send -> via inbox
[t+ 20.8s] receipt: delivered by the PostToolUse hook
[t+ 26.9s] Claude replied: 'DONE MANGO-A'
[t+ 26.9s] verdict: PASS
[t+ 26.9s] === B: delivery on UserPromptSubmit
[t+ 26.9s] cxd send -> via inbox
[t+ 29.9s] while idle, 1 message(s) wait in the inbox
[t+ 31.4s] receipt: delivered by the UserPromptSubmit hook
[t+ 35.4s] Claude replied: 'READY KIWI-B'; Stop hook records this turn: 1
[t+ 35.4s] verdict: PASS
[t+ 35.4s] === C: delivery at Stop, and whether the session continues
[t+ 38.0s] cxd send -> via inbox
[t+ 44.0s] receipt: delivered by the Stop hook
[t+ 48.0s] assistant text blocks this case: 2; Stop hook runs this case: 2
[t+ 48.0s] Claude's follow-up: 'LIME-C1'
[t+ 48.0s] verdict: PASS: delivered at Stop; the session continued and acted on it
[t+ 48.0s] === D: a message queued while the session is idle
[t+ 48.1s] cxd send -> via inbox
[t+ 78.1s] after 30s idle: not delivered, still queued (1 pending)
[t+ 81.6s] the next prompt delivered it (receipt: UserPromptSubmit hook); Claude replied: 'OK PLUM-D'
[t+ 81.6s] verdict: NOT DELIVERED WHILE IDLE (expected without a waker); delivered by the next prompt
[t+ 85.1s] /exit -> SessionEnd hook marked the session ended (reason=prompt_input_exit)
[t+ 85.1s] claude process is gone
[t+ 85.1s] summary: {"A": "PASS", "B": "PASS", "C": "PASS: delivered at Stop; the session continued and acted on it", "D": "NOT DELIVERED WHILE IDLE (expected without a waker); delivered by the next prompt"}
```
