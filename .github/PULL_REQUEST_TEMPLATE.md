## What changed

<!-- One or two sentences. Link the issue if there is one. -->

## Why

## How I verified it

<!-- Paste the counts, not adjectives. -->

- [ ] `python3 -m unittest discover -s tests`: `Ran N tests ... OK`
- [ ] `python3 tests/mutate.py`: `N mutation(s), 0 failure(s)`
- [ ] `scripts/check-secrets.sh --self-test` and `scripts/check-secrets.sh --all` pass
- [ ] New guard? It has a test **and** a mutation in `tests/mutate.py` that turns it red
- [ ] Docs (README, SKILL.md) updated if behaviour or flags changed

## Checklist

- [ ] No tokens, keys, `.env` files, logs with secrets, machine names, home paths or IPs
- [ ] Standard library only, works on macOS and Linux
