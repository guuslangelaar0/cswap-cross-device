#!/usr/bin/env python3
"""Check that the marketplace installs the latest release, pinned by commit.

Every plugin entry in .claude-plugin/marketplace.json must have a git `url`
source pinned to a release tag (`ref`: vX.Y.Z) and to that tag's commit
(`sha`), where:

  - the tag exists (refs/tags/vX.Y.Z) and `sha` is its commit;
  - it is the newest vX.Y.Z tag merged into HEAD;
  - its commit is part of the history being checked (HEAD), so it was merged;
  - the plugin.json at that commit has version X.Y.Z and the entry's name;
  - `url` is that plugin.json's `repository`, over https (no ssh key needed).

So `claude plugin install` gets exactly the tagged release, never whatever
`main` holds. Release tags are protected by a repository ruleset (no moves or
deletions); a moved tag would turn this check red.

    python3 scripts/check-release-pin.py [--repo DIR]

Needs the release tags in the clone (CI checks out with fetch-depth 0).
Exit 0 when every entry is consistent, 1 otherwise.
"""

import json
import os
import subprocess
import sys

DIGITS = "0123456789"


def release_version(ref):
    """(X, Y, Z) for a release tag "vX.Y.Z" with plain ASCII digits, else None."""
    if not isinstance(ref, str) or not ref.startswith("v"):
        return None
    parts = ref[1:].split(".")
    if len(parts) != 3 or not all(p and all(c in DIGITS for c in p) and (p == "0" or p[0] != "0") for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _is_sha(s):
    return isinstance(s, str) and len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def git_env():
    """git must look at --repo only: drop GIT_DIR and friends (set inside git
    hooks and worktrees) and ignore global and system config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GUARD:release-pin-git-env
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


_last_git_error = [""]


def _git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, env=git_env())
    _last_git_error[0] = " ".join(r.stderr.split())[:200] if r.returncode else ""
    return r.returncode, r.stdout.strip()


def _why():
    return " (git: %s)" % _last_git_error[0] if _last_git_error[0] else ""


def _norm_url(u):
    u = u.rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def check_entry(repo, i, entry, newest):
    if not isinstance(entry, dict):
        return ["plugins[%d]: not an object" % i]
    name = entry.get("name")
    where = "plugins[%d] (%s)" % (i, name)
    problems = []
    if "version" in entry:
        problems.append("%s: drop 'version' from the entry; plugin.json at the pinned tag is the version" % where)
    src = entry.get("source")
    if not isinstance(src, dict) or src.get("source") != "url":
        return problems + ["%s: source must be a git url source pinned to a release tag, got %r" % (where, src)]
    url, ref, sha = src.get("url"), src.get("ref"), src.get("sha")
    if not isinstance(url, str) or not url.startswith("https://"):  # GUARD:release-pin-https
        problems.append("%s: url must be an https:// git URL (installs need no ssh key), got %r" % (where, url))
    if release_version(ref) is None:
        return problems + ["%s: ref must be a release tag vX.Y.Z, got %r" % (where, ref)]
    if not _is_sha(sha):
        return problems + ["%s: sha must be the 40-character commit of %s, got %r" % (where, ref, sha)]
    rc, tagged = _git(repo, "rev-parse", "--verify", "-q", "refs/tags/%s^{commit}" % ref)
    if rc != 0:
        return problems + ["%s: tag %s does not exist in this clone (fetch the tags)%s" % (where, ref, _why())]
    if tagged != sha:  # GUARD:release-pin-sha
        return problems + ["%s: sha %s is not the commit of %s (%s)" % (where, sha, ref, tagged)]
    if newest and release_version(ref) != newest:  # GUARD:release-pin-latest
        problems.append("%s: pins %s, but the newest release tag is v%s" % (where, ref, ".".join(map(str, newest))))
    rc, _ = _git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    if rc != 0:  # GUARD:release-pin-merged
        problems.append("%s: %s (%s) is not in the history of HEAD: tag a merged commit%s" % (
            where, ref, sha[:12], _why()))
    rc, raw = _git(repo, "show", sha + ":.claude-plugin/plugin.json")
    try:
        manifest = json.loads(raw) if rc == 0 else None
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict):
        return problems + ["%s: no readable .claude-plugin/plugin.json at %s%s" % (where, ref, _why())]
    if manifest.get("version") != ref[1:]:  # GUARD:release-pin-version
        problems.append("%s: plugin.json at %s says version %r, the tag says %s" % (
            where, ref, manifest.get("version"), ref[1:]))
    if manifest.get("name") != name:
        problems.append("%s: plugin.json at %s is named %r" % (where, ref, manifest.get("name")))
    repository = manifest.get("repository")
    if isinstance(url, str) and (not isinstance(repository, str) or _norm_url(url) != _norm_url(repository)):
        problems.append("%s: url %s is not the plugin's repository %r" % (where, url, repository))
    return problems


def check(repo):
    try:
        with open(os.path.join(repo, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            market = json.load(f)
    except (OSError, ValueError) as e:
        return ["marketplace.json: %s" % e]
    if not isinstance(market, dict) or not isinstance(market.get("plugins"), list) or not market["plugins"]:
        return ["marketplace.json must be an object with a non-empty 'plugins' list"]
    rc, tags = _git(repo, "tag", "--merged", "HEAD", "--list", "v*")  # tags on unmerged commits do not count
    versions = [v for v in (release_version(t) for t in tags.split()) if v] if rc == 0 else []
    newest = max(versions) if versions else None
    problems = []
    for i, entry in enumerate(market["plugins"]):
        problems += check_entry(repo, i, entry, newest)
    return problems


def main(argv):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if len(argv) == 2 and argv[0] == "--repo":
        repo = argv[1]
    elif argv:
        sys.stderr.write("usage: check-release-pin.py [--repo DIR]\n")
        return 2
    problems = check(repo)
    for p in problems:
        print("release pin: " + p)
    if not problems:
        print("release pin: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
