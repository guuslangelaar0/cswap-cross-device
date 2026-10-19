# Evidence

Runs of real Claude Code that prove documented features, beyond the stub-based test suite.

**Redaction.** The logs contain no host names, user names, account names, session ids or paths. Drivers redact them before writing:
- session ids become `<sid>` or `<uuid>`;
- home paths become `~`;
- accounts become `<account>`.

**Cost.** Each run uses tiny prompts on a small model, and every process it starts is stopped at the end.

| Date | Feature | Platform | Result | Log |
| --- | --- | --- | --- | --- |
| 2026-10-19 | Hook delivery into an interactive session, and the idle waker: after a turn (D) and in a session nobody typed into (E) | Linux, Claude Code 2.1.303 | A, B, C, D, E pass | [log](2026-10-19-tui-delivery-waker-linux.md) |
| 2026-10-18 | Hook delivery into an interactive session: PostToolUse, UserPromptSubmit, Stop (and the session continues), idle | Linux, Claude Code 2.1.302 | A, B, C pass; D: idle sessions waited for the next prompt (before the idle waker) | [log](2026-10-18-tui-delivery-linux.md) |

Drivers live in `scripts/evidence/`.
