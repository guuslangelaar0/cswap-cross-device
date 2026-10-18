# Security policy

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's private vulnerability
reporting:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** to open a private security advisory.

Do not open a public issue, pull request or discussion for a vulnerability. Once a fix is
released, the advisory is published with credit to you, unless you ask otherwise.

## Never post secrets

Never paste tokens, API keys, ssh keys, OAuth credentials or logs that contain them into an
issue, a pull request or an advisory. `~/.cxd/` and Claude Code's own logs can contain
message text, session ids and file paths: redact them before you share an excerpt. If you
posted a secret by accident, revoke it first, then ask for the post to be removed.

## Scope

In scope: anything in this repository, for example a way for another local user or a remote
host to write to your inbox or FIFO, to make `cxd` signal a process it did not start, to
inject text into a session through the hooks, or to read message or report files.

Out of scope: Claude Code itself, cswap (claude-swap), and your ssh configuration. Report
those to their own maintainers.

## Supported versions

Security fixes go into the latest release only.
