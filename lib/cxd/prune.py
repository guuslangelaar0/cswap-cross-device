"""Pruning: remove what ended long ago, never anything alive.

A session is pruned once it has ended (SessionEnd), or its recorded process is
gone, and nothing of it (record, inbox, claimed, delivered, outbox) has changed
for `prune_after_days` (config.json, default 7). A session whose process was
never recorded is kept until it ends: it cannot be proven dead. A session that
belongs to an existing run (its record says so, or the run's meta.json names
it) stays until the run itself is pruned. Runs are pruned only on request
(`cxd prune --runs`), once their claude, keeper and supervisor are all gone and
nothing in the run directory has changed for the same age; both checks are
repeated right before the removal.

The SessionStart hook prunes sessions at most once an hour (prune.stamp), after
it has delivered any mail, and removes at most AUTO_LIMIT sessions per pass, so
state stays bounded without anyone running `cxd prune`. Imports stay light:
this runs inside a hook.
"""

import math
import os
import time

from .common import CxdError, path, proc_alive, read_json

DEFAULT_DAYS = 7.0
AUTO_EVERY = 3600.0
AUTO_LIMIT = 200
_SESSION_PARTS = (("sessions", "%s.json"), ("outbox", "%s.jsonl"), ("inbox", "%s"),
                  ("claimed", "%s"), ("delivered", "%s"))


def _valid_days(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0


def age_days(cfg, override=None):
    """The age to prune at. A bad override is an error; a bad config value
    means the default (a broken config never breaks anything)."""
    if override is not None:
        if not _valid_days(override):
            raise CxdError("--older-than must be a positive number of days, got %r" % override, "usage")
        return float(override)
    v = (cfg or {}).get("prune_after_days")
    return float(v) if _valid_days(v) else DEFAULT_DAYS


def _last_change(paths):
    newest = 0.0
    for p in paths:
        try:
            newest = max(newest, os.lstat(p).st_mtime)
        except OSError:
            continue
        if os.path.isdir(p) and not os.path.islink(p):
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
    pids = read_json(os.path.join(d, "pids.json"), {})
    if not isinstance(pids, dict):
        return True  # unreadable: assume alive
    return any(proc_alive(pids.get(k + "_pid"), pids.get(k + "_ident")) for k in ("claude", "keeper", "supervisor"))


def _record(sid):
    rec = read_json(path("sessions", sid + ".json"), None)
    return rec if isinstance(rec, dict) else {}


def _prunable_session(sid, cutoff):
    if _last_change(_session_paths(sid)) >= cutoff:  # GUARD:prune-age
        return False
    rec = _record(sid)
    if rec.get("ended"):
        return True
    if not rec.get("pid") and os.path.exists(path("sessions", sid + ".json")):  # GUARD:prune-unknown
        return False  # never recorded its process: cannot prove it is gone
    return not proc_alive(rec.get("pid"), rec.get("pid_ident"))  # GUARD:prune-live


def _stale_temp_files(cutoff):
    out = []
    for box in ("sessions", "outbox"):
        try:
            names = os.listdir(path(box))
        except OSError:
            continue
        for n in names:
            p = path(box, n)
            if ".tmp." in n and _last_change([p]) < cutoff:
                out.append(p)
    return out


def prune(cfg, older_than_days=None, runs=False, dry_run=False, limit=None):
    days = age_days(cfg, older_than_days)
    cutoff = time.time() - days * 86400
    out = {"older_than_days": days, "sessions": [], "runs": [], "dry_run": bool(dry_run)}
    try:
        run_names = sorted(n for n in os.listdir(path("runs")) if os.path.isdir(path("runs", n)))
    except FileNotFoundError:
        run_names = []
    # (Any other error listing runs/ propagates: without the runs, their
    # sessions could not be protected.)
    run_of = {}
    for name in run_names:
        meta = read_json(path("runs", name, "meta.json"), {})
        if isinstance(meta, dict) and meta.get("session_id"):
            run_of[meta["session_id"]] = name
    doomed_runs = set()
    if runs:
        for name in run_names:
            d = path("runs", name)
            if _last_change([d]) < cutoff and not _run_alive(d):
                doomed_runs.add(name)
    for sid in sorted(_session_ids()):
        if limit is not None and len(out["sessions"]) >= limit:
            break
        if not _prunable_session(sid, cutoff):
            continue
        run = _record(sid).get("cxd_run") or run_of.get(sid)
        if run and run in run_names and run not in doomed_runs:
            continue  # goes with its run
        if not dry_run:
            if not _prunable_session(sid, cutoff):  # re-check: a resume may have touched it
                continue
            for p in _session_paths(sid):
                _remove(p)
        out["sessions"].append(sid)
    for name in sorted(doomed_runs):
        d = path("runs", name)
        if not dry_run:
            if _last_change([d]) >= cutoff or _run_alive(d):  # GUARD:prune-run-recheck
                continue  # resumed meanwhile
            _remove(d)
        out["runs"].append(name)
    if not dry_run:
        for p in _stale_temp_files(cutoff):
            _remove(p)
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
        with open(stamp, "a"):
            pass
        os.utime(stamp, None)
    except OSError:
        return None
    return prune(cfg, limit=AUTO_LIMIT)
