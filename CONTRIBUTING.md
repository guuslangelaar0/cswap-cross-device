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
   ```

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

## Secrets

`scripts/check-secrets.sh` blocks a commit that adds private keys, GitHub tokens, Anthropic
or OpenAI-style keys, AWS keys, `.env` files or long base64 blobs. If it flags something by
mistake, change the content rather than bypassing the check, or explain the false positive
in your pull request. Never use `git commit --no-verify` to get a secret past it.

## Reporting bugs and security problems

Use the issue templates for bugs and feature requests. Report vulnerabilities privately, as
described in [SECURITY.md](SECURITY.md).

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE)
and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
