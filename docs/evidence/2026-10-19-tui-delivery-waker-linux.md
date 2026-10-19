# Evidence: delivery into an interactive session, with the idle waker (Linux)

Driver: `scripts/evidence/tui_delivery.py --cases ABCDEF --model haiku` with the plugin from this repository at commit e17b486 (`--plugin-dir`), on an account where no other copy of the plugin is installed. Real Claude Code in a private tmux server, a throwaway `CXD_HOME`, redacted.

- A-C run with the waker off (`"idle_wake": false`): they prove the hooks.
- D: after a turn, a message to the idle session is delivered by the waker its `Stop` started, and Claude acts on it with no prompt typed.
- E: after `/clear`, a message to the new session, which nobody has typed into, is delivered by the waker its `SessionStart` started.
- F: a turn interrupted with Esc ends without a `Stop`, so the session counts as busy: a message sent then is not delivered while it waits, and the next prompt delivers it. This is the documented limitation, shown as it is.

All six cases pass.

```
[t+  0.2s] Claude Code 2.1.303 (as launched by cswap); cxd 0.1.0; plugin tree at e17b486c61c3; Linux x86_64; python 3.14.7; model haiku
[t+  0.3s] started claude TUI in a private tmux server: cswap run <account> -- --plugin-dir <plugin-dir> --model haiku --allowedTools Bash(sleep *)
[t+  2.3s] workspace trust dialog shown; choosing 'Yes, I trust this folder'
[t+  2.8s] SessionStart hook registered the session (pid recorded: True)
[t+  3.4s] cxd list shows the session alive=True
[t+  3.4s] === A: delivery on PostToolUse
[t+  9.0s] cxd send -> via inbox
[t+ 20.0s] receipt: delivered by the PostToolUse hook
[t+ 25.0s] Claude replied: 'DONE\nMANGO-A'
[t+ 25.0s] verdict: PASS
[t+ 25.0s] === B: delivery on UserPromptSubmit
[t+ 25.0s] cxd send -> via inbox
[t+ 28.0s] while idle, 1 message(s) wait in the inbox
[t+ 29.5s] receipt: delivered by the UserPromptSubmit hook
[t+ 32.9s] Claude replied: 'READY\nKIWI-B'; Stop hook records this turn: 1
[t+ 32.9s] verdict: PASS
[t+ 32.9s] === C: delivery at Stop, and whether the session continues
[t+ 35.4s] cxd send -> via inbox
[t+ 39.4s] receipt: delivered by the Stop hook
[t+ 43.5s] assistant text blocks this case: 2; Stop hook runs this case: 2
[t+ 43.5s] Claude's follow-up: 'LIME-C1'
[t+ 43.5s] verdict: PASS: delivered at Stop; the session continued and acted on it
[t+ 43.5s] === D: a message queued while the session is idle (the waker a Stop started)
[t+ 51.0s] cxd send -> via inbox
[t+ 52.0s] receipt: delivered by the Wake hook while idle (no prompt typed)
[t+ 57.0s] Claude replied, unprompted: 'PLUM-D'
[t+ 57.0s] verdict: PASS: delivered while idle by the waker; Claude acted on it without a prompt
[t+ 59.0s] === E: a message to a session nobody has typed into (the waker SessionStart started)
[t+ 60.5s] /clear -> SessionEnd for the old session, SessionStart registered the new one
[t+ 63.6s] cxd send -> via inbox
[t+ 64.6s] receipt: delivered by the Wake hook; no prompt has been typed in this session
[t+ 67.6s] Claude replied, unprompted: 'FIG-E'
[t+ 67.6s] verdict: PASS: the SessionStart waker delivered to an untouched session; Claude acted on it
[t+ 69.6s] === F: a message after a turn interrupted with Esc (waits for the next prompt)
[t+ 78.4s] pressed Esc while the answer streamed; the turn ended without a Stop record (interrupted)
[t+ 78.4s] cxd send -> via inbox
[t+123.4s] after 45s: not delivered (the session counts as busy after Esc), still queued
[t+127.9s] the next prompt delivered it (receipt: UserPromptSubmit hook); Claude replied: 'PEAR-F'
[t+127.9s] verdict: PASS: after Esc the message waited for the next prompt, which delivered it (documented limitation)
[t+131.5s] /exit -> SessionEnd hook marked the session ended (reason=prompt_input_exit)
[t+131.5s] claude process is gone
[t+131.5s] summary: {"A": "PASS", "B": "PASS", "C": "PASS: delivered at Stop; the session continued and acted on it", "D": "PASS: delivered while idle by the waker; Claude acted on it without a prompt", "E": "PASS: the SessionStart waker delivered to an untouched session; Claude acted on it", "F": "PASS: after Esc the message waited for the next prompt, which delivered it (documented limitation)"}
```
