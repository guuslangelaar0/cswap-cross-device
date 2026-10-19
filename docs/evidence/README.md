# Evidence

Runs of real Claude Code that prove documented features, beyond the stub-based test suite.

**Redaction.** The logs contain no host names, user names, account names, session ids or paths. Drivers redact them before writing:
- session ids become `<sid>` or `<uuid>`;
- home paths become `~`;
- accounts become `<account>`.

**Cost.** Each run uses tiny prompts on a small model, and every process it starts is stopped at the end.

| Date | Feature | Platform | Result | Log |
| --- | --- | --- | --- | --- |
| 2026-10-19 | Secret scans: gitleaks over every commit and the tree, and the repository's own check | Linux, macOS | clean | [log](2026-10-19-gitleaks.md) |
| 2026-10-18 | Hook delivery into an interactive session: PostToolUse, UserPromptSubmit, Stop (and the session continues), idle | Linux, Claude Code 2.1.302 | A, B, C pass; D: idle sessions wait for the next prompt | [log](2026-10-18-tui-delivery-linux.md) |

Drivers live in `scripts/evidence/`.
