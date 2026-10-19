# Contributing

Thanks for helping. Changes come in through forks and pull requests; nobody pushes to
`main` directly, and `main` only accepts a pull request whose CI is green.

## Workflow

1. **Fork** the repository on GitHub and clone your fork.
2. **Branch** from `main`: `git switch -c fix/short-description`.
3. **Install the secret check** as a pre-commit hook (once per clone):

   ```bash
   ln -sf ../../scripts/check-secrets.sh .git/hooks/pre-commit
   ```

4. Make your change, with tests (see below).
5. **Run everything** that CI runs:

   ```bash
   python3 -m unittest discover -s tests
   python3 tests/mutate.py
   scripts/check-secrets.sh --self-test
   scripts/check-secrets.sh --all
   python3 scripts/check-release-pin.py
   ```

   In a fork, the pin check needs this repository's release tags:
   `git fetch --tags https://github.com/guuslangelaar0/cswap-cross-device.git`.

   To try your change in Claude Code itself, load your working tree as a plugin:
   `claude --plugin-dir .` (with cswap: `cswap run <account> -- --plugin-dir .`). The
   marketplace installs releases only, so adding it does not test your branch.

6. **Open a pull request** against `main`. Fill in the template: what changed, why, and how
   you verified it.

## Tests

- The tests use a stub `claude` (`tests/fakes/claude_stub.py`), a fake `cswap` and a fake
  `ssh`. They need no Claude account, no network and no cost. Never make a test call the
  real `claude`.
- Every behaviour change needs a test that fails without the change.
- **A new guard needs a mutation.** A guard is any check that refuses, protects or proves
  something (a liveness check, a refusal, a "never report X unless Y"). Add an entry to
  `MUTATIONS` in `tests/mutate.py` that removes or breaks the guard, and make sure
  `python3 tests/mutate.py <name>` reports the mutated run as RED. A test that has never been
  seen to fail is not trusted to pass.
- Keep the hooks fast and silent: `test_hook_is_fast` and `test_hook_never_breaks_a_session`
  must stay green.

## Code

- Python 3.9+, standard library only. No third-party dependencies.
- macOS and Linux only. Do not assume GNU tools on macOS or BSD tools on Linux.
- Signal only processes `cxd` started itself, and re-check their identity first. Never kill
  by name.
- No machine names, account names, user names, home paths or IP addresses in code, tests or
  docs. Use the neutral examples `laptop`, `buildbox`, `work`, `personal` and `w1`.

## Releasing

The marketplace installs the latest release, never `main`: its plugin entry has a git `url`
source pinned to the release tag (`ref`) and to that tag's commit (`sha`).
`scripts/check-release-pin.py` (run in CI) fails unless:
- the tag exists and is the newest `vX.Y.Z` tag;
- the `sha` is its commit, and that commit is merged;
- the `plugin.json` at that commit carries the tag's version, the entry's name and the
  entry's `url` as its `repository`.

Release tags are protected by a ruleset (no updates or deletions). A release takes two pull
requests, with the tag between them:

1. **Bump.** Set the new version in `.claude-plugin/plugin.json` and in `VERSION` in
   `lib/cxd/common.py` (a test checks that they agree). Merge when CI is green.
2. **Tag** (not a pull request). Create the release on the merge commit, with authored notes
   (an intro, What's New, Fixes, Verification, Install/Upgrade). Until step 3 lands, the pin
   check fails on every pull request, because a newer release exists than the one pinned:
   `gh release create vX.Y.Z --target <full merge sha> --title "vX.Y.Z: …" --notes-file notes.md`
3. **Pin.** Point the marketplace entry at the release: `ref` `vX.Y.Z` and `sha` the full
   commit of the tag. Merge when CI is green. Installs and `claude plugin update` pick the
   release up from here, so merge this one right after the tag.

Tags cannot be moved or deleted. If a release turns out wrong (bad code, or a tag on the
wrong commit), do not try to repair it: release a fixed `vX.Y.Z+1` the same way, which also
unblocks the pin check. Only tags merged into the checked history count as "newest".

## Secrets

`scripts/check-secrets.sh` blocks a commit that adds private keys, GitHub tokens, Anthropic
or OpenAI-style keys, AWS keys, `.env` files or long base64 blobs. If it flags something by
mistake, change the content rather than bypassing the check, or explain the false positive
in your pull request. Never use `git commit --no-verify` to get a secret past it.

Every commit of every branch is also scanned with [gitleaks](https://github.com/gitleaks/gitleaks):
`scripts/gitleaks.sh` downloads a pinned release into a throwaway directory, checks its SHA-256
and runs it over the whole history (Linux x64 and macOS arm64).

## Reporting bugs and security problems

Use the issue templates for bugs and feature requests. Report vulnerabilities privately, as
described in [SECURITY.md](SECURITY.md).

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE)
and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
