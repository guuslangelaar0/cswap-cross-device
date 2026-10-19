# Evidence: delivery into an interactive session, with the idle waker (Linux)

Driver: `scripts/evidence/tui_delivery.py --cases ABCDEF --model haiku` with the plugin from this repository at commit f51e2c6 (`--plugin-dir`), on an account where no other copy of the plugin is installed. Real Claude Code in a private tmux server, a throwaway `CXD_HOME`, redacted.

- A-C run with the waker off (`"idle_wake": false`): they prove the hooks.
- D: after a turn, a message to the idle session is delivered by the waker its `Stop` started, and Claude acts on it with no prompt typed.
- E: after `/clear`, a message to the new session, which nobody has typed into, is delivered by the waker its `SessionStart` started.
- F: a turn interrupted with Esc ends without a `Stop`, so the session counts as busy: a message sent then is not delivered while it waits, and the next prompt delivers it. This is the documented limitation, shown as it is.

All six cases pass.

```
[t+  0.1s] Claude Code 2.1.303 (as launched by cswap); cxd 0.1.0; plugin tree at f51e2c6440c9; Linux x86_64; python 3.14.7; model haiku
[t+  0.1s] started claude TUI in a private tmux server: cswap run <account> -- --plugin-dir <plugin-dir> --model haiku --allowedTools Bash(sleep *)
[t+  1.6s] workspace trust dialog shown; choosing 'Yes, I trust this folder'
[t+  2.2s] SessionStart hook registered the session (pid recorded: True)
[t+  2.7s] cxd list shows the session alive=True
[t+  2.7s] === A: delivery on PostToolUse
[t+  8.2s] cxd send -> via inbox
[t+ 18.3s] receipt: delivered by the PostToolUse hook
[t+ 24.3s] Claude replied: 'MANGO-A'
[t+ 24.3s] verdict: PASS
[t+ 24.3s] === B: delivery on UserPromptSubmit
[t+ 24.3s] cxd send -> via inbox
[t+ 27.3s] while idle, 1 message(s) wait in the inbox
[t+ 28.8s] receipt: delivered by the UserPromptSubmit hook
[t+ 32.8s] Claude replied: 'READY\n\nKIWI-B'; Stop hook records this turn: 1
[t+ 32.8s] verdict: PASS
[t+ 32.8s] === C: delivery at Stop, and whether the session continues
[t+ 35.4s] cxd send -> via inbox
[t+ 41.4s] receipt: delivered by the Stop hook
[t+ 45.4s] assistant text blocks this case: 2; Stop hook runs this case: 2
[t+ 45.4s] Claude's follow-up: 'LIME-C1'
[t+ 45.4s] verdict: PASS: delivered at Stop; the session continued and acted on it
[t+ 45.4s] === D: a message queued while the session is idle (the waker a Stop started)
[t+ 52.0s] cxd send -> via inbox
[t+ 53.0s] receipt: delivered by the Wake hook while idle (no prompt typed)
[t+ 55.0s] Claude replied, unprompted: 'PLUM-D'
[t+ 55.0s] verdict: PASS: delivered while idle by the waker; Claude acted on it without a prompt
[t+ 57.0s] === E: a message to a session nobody has typed into (the waker SessionStart started)
[t+ 58.5s] /clear -> SessionEnd for the old session, SessionStart registered the new one
[t+ 61.6s] cxd send -> via inbox
[t+ 62.6s] receipt: delivered by the Wake hook; no prompt has been typed in this session
[t+ 65.6s] Claude replied, unprompted: 'FIG-E'
[t+ 65.6s] verdict: PASS: the SessionStart waker delivered to an untouched session; Claude acted on it
[t+ 67.6s] === F: a message after a turn interrupted with Esc (waits for the next prompt)
[t+ 76.5s] pressed Esc while the answer streamed; the turn ended without a Stop record (interrupted)
[t+ 76.6s] cxd send -> via inbox
[t+121.6s] after 45s: not delivered (the session counts as busy after Esc), still queued
[t+133.1s] the next prompt delivered it (receipt: UserPromptSubmit hook); Claude replied: 'PEAR-F'
[t+133.1s] verdict: PASS: after Esc the message waited for the next prompt, which delivered it (documented limitation)
[t+136.7s] /exit -> SessionEnd hook marked the session ended (reason=prompt_input_exit)
[t+136.7s] claude process is gone
[t+136.7s] summary: {"A": "PASS", "B": "PASS", "C": "PASS: delivered at Stop; the session continued and acted on it", "D": "PASS: delivered while idle by the waker; Claude acted on it without a prompt", "E": "PASS: the SessionStart waker delivered to an untouched session; Claude acted on it", "F": "PASS: after Esc the message waited for the next prompt, which delivered it (documented limitation)"}
```
