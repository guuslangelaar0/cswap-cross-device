"""Pruning: remove what ended long ago, never anything alive.

A session is pruned once it has ended (SessionEnd) or its process is gone, and
nothing of it (record, inbox, claimed, delivered, outbox) has changed for
`prune_after_days` (config.json, default 7). A session that belongs to an
existing run stays until the run itself is pruned. Runs are pruned only on
request (`cxd prune --runs`), once their claude, keeper and supervisor are
all gone and nothing in the run directory has changed for the same age.

The SessionStart hook prunes sessions at most once an hour (prune.stamp), so
state stays bounded without anyone running `cxd prune`. Imports stay light:
this runs inside a hook.
"""

import os
import time

from .common import path, proc_alive, read_json

DEFAULT_DAYS = 7.0
AUTO_EVERY = 3600.0
_SESSION_PARTS = (("sessions", "%s.json"), ("outbox", "%s.jsonl"), ("inbox", "%s"),
                  ("claimed", "%s"), ("delivered", "%s"))


def age_days(cfg, override=None):
    """The configured age; anything that is not a positive number means the default."""
    v = override if override is not None else (cfg or {}).get("prune_after_days")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return DEFAULT_DAYS
    return float(v)


def _last_change(paths):
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, os.stat(p).st_mtime)
        except OSError:
            continue
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                for n in dirs + files:
                    try:
                        newest = max(newest, os.lstat(os.path.join(root, n)).st_mtime)
                    except OSError:
                        pass
    return newest


def _session_paths(sid):
    return [path(box, pattern % sid) for box, pattern in _SESSION_PARTS]


def _remove(p):
    import shutil
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except OSError:
        pass


def _session_ids():
    ids = set()
    for box, pattern in _SESSION_PARTS:
        suffix = pattern.replace("%s", "")
        try:
            names = os.listdir(path(box))
        except OSError:
            continue
        for n in names:
            if n.startswith(".") or (suffix and not n.endswith(suffix)):
                continue
            ids.add(n[: len(n) - len(suffix)] if suffix else n)
    return ids


def _run_alive(d):
    pids = read_json(os.path.join(d, "pids.json"), {}) or {}
    return any(proc_alive(pids.get(k + "_pid"), pids.get(k + "_ident")) for k in ("claude", "keeper", "supervisor"))


def prune(cfg, older_than_days=None, runs=False, dry_run=False):
    days = age_days(cfg, older_than_days)
    cutoff = time.time() - days * 86400
    out = {"older_than_days": days, "sessions": [], "runs": [], "dry_run": bool(dry_run)}
    run_names = set()
    try:
        run_names = set(n for n in os.listdir(path("runs")) if os.path.isdir(path("runs", n)))
    except OSError:
        pass
    doomed_runs = set()
    if runs:
        for name in sorted(run_names):
            d = path("runs", name)
            if _last_change([d]) < cutoff and not _run_alive(d):
                doomed_runs.add(name)
    for sid in sorted(_session_ids()):
        paths = _session_paths(sid)
        if _last_change(paths) >= cutoff:  # GUARD:prune-age
            continue
        rec = read_json(paths[0], None) or {}
        if not rec.get("ended") and proc_alive(rec.get("pid"), rec.get("pid_ident")):  # GUARD:prune-live
            continue
        run = rec.get("cxd_run")
        if run and run in run_names and run not in doomed_runs:
            continue  # goes with its run
        out["sessions"].append(sid)
        if not dry_run and _last_change(paths) < cutoff:  # re-check: a resume may have touched it
            for p in paths:
                _remove(p)
    for name in sorted(doomed_runs):
        out["runs"].append(name)
        if not dry_run:
            _remove(path("runs", name))
    return out


def maybe_prune(cfg):
    """From the SessionStart hook: prune sessions at most once an hour."""
    stamp = path("prune.stamp")
    try:
        if time.time() - os.stat(stamp).st_mtime < AUTO_EVERY:
            return None
    except OSError:
        pass
    try:
        with open(stamp, "w"):
            pass
    except OSError:
        return None
    return prune(cfg)
