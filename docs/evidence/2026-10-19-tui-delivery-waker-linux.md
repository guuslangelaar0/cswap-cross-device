# Evidence: delivery into an interactive session, with the idle waker (Linux)

Driver: `scripts/evidence/tui_delivery.py --cases ABCDE --model haiku` with the plugin from this repository at commit c98b715 (`--plugin-dir`), on an account where no other copy of the plugin is installed. Real Claude Code in a private tmux server, a throwaway `CXD_HOME`, redacted.

- A-C run with the waker off (`"idle_wake": false`): they prove the hooks.
- D: after a turn, a message to the idle session is delivered by the waker its `Stop` started, and Claude acts on it with no prompt typed.
- E: after `/clear`, a message to the new session, which nobody has typed into, is delivered by the waker its `SessionStart` started.

All five cases pass.

```
[t+  0.1s] Claude Code 2.1.303 (as launched by cswap); cxd 0.1.0; plugin tree at c98b7151758e; Linux x86_64; python 3.14.7; model haiku
[t+  0.1s] started claude TUI in a private tmux server: cswap run <account> -- --plugin-dir <plugin-dir> --model haiku --allowedTools Bash(sleep *)
[t+  1.6s] workspace trust dialog shown; choosing 'Yes, I trust this folder'
[t+  2.1s] SessionStart hook registered the session (pid recorded: True)
[t+  2.7s] cxd list shows the session alive=True
[t+  2.7s] === A: delivery on PostToolUse
[t+  8.2s] cxd send -> via inbox
[t+ 19.2s] receipt: delivered by the PostToolUse hook
[t+ 24.3s] Claude replied: 'DONE MANGO-A'
[t+ 24.3s] verdict: PASS
[t+ 24.3s] === B: delivery on UserPromptSubmit
[t+ 24.3s] cxd send -> via inbox
[t+ 27.3s] while idle, 1 message(s) wait in the inbox
[t+ 28.8s] receipt: delivered by the UserPromptSubmit hook
[t+ 32.8s] Claude replied: 'READY KIWI-B'; Stop hook records this turn: 1
[t+ 32.8s] verdict: PASS
[t+ 32.8s] === C: delivery at Stop, and whether the session continues
[t+ 35.4s] cxd send -> via inbox
[t+ 40.4s] receipt: delivered by the Stop hook
[t+ 44.4s] assistant text blocks this case: 2; Stop hook runs this case: 2
[t+ 44.4s] Claude's follow-up: 'LIME-C1'
[t+ 44.4s] verdict: PASS: delivered at Stop; the session continued and acted on it
[t+ 44.4s] === D: a message queued while the session is idle (the waker a Stop started)
[t+ 51.0s] cxd send -> via inbox
[t+ 52.0s] receipt: delivered by the Wake hook while idle (no prompt typed)
[t+ 54.0s] Claude replied, unprompted: 'PLUM-D'
[t+ 54.0s] verdict: PASS: delivered while idle by the waker; Claude acted on it without a prompt
[t+ 56.0s] === E: a message to a session nobody has typed into (the waker SessionStart started)
[t+ 57.5s] /clear -> SessionEnd for the old session, SessionStart registered the new one
[t+ 60.5s] cxd send -> via inbox
[t+ 61.5s] receipt: delivered by the Wake hook; no prompt has been typed in this session
[t+ 63.5s] Claude replied, unprompted: 'FIG-E'
[t+ 63.5s] verdict: PASS: the SessionStart waker delivered to an untouched session; Claude acted on it
[t+ 67.1s] /exit -> SessionEnd hook marked the session ended (reason=prompt_input_exit)
[t+ 67.1s] claude process is gone
[t+ 67.1s] summary: {"A": "PASS", "B": "PASS", "C": "PASS: delivered at Stop; the session continued and acted on it", "D": "PASS: delivered while idle by the waker; Claude acted on it without a prompt", "E": "PASS: the SessionStart waker delivered to an untouched session; Claude acted on it"}
```
