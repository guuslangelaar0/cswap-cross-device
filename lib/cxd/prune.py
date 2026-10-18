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

The SessionStart hook starts a detached pruning pass at most once an hour
(prune.stamp), which removes at most AUTO_LIMIT sessions, so state stays
bounded without anyone running `cxd prune` and no session waits for it.
"""

import math
import os
import re
import time

from .common import CxdError, path, proc_alive, read_json

DEFAULT_DAYS = 7.0
AUTO_EVERY = 3600.0
AUTO_LIMIT = 200
_SESSION_PARTS = (("sessions", "%s.json"), ("outbox", "%s.jsonl"), ("inbox", "%s"),
                  ("claimed", "%s"), ("delivered", "%s"))


def _valid_days(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v) and v > 0 and v * 86400 < 1e12
    except OverflowError:
        return False


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
    """Remove p; True when it is gone afterwards."""
    import shutil
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return not os.path.lexists(p)


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
    """False only when the run is provably over: all its processes are recorded
    and gone. A run without pids.json counts as over only if its start failed
    (an exit record); anything else unknown counts as alive."""
    pids = read_json(os.path.join(d, "pids.json"), None)
    if pids is None:
        return not os.path.exists(os.path.join(d, "exit.json"))
    if not isinstance(pids, dict):
        return True
    for k in ("claude", "keeper", "supervisor"):
        pid, ident = pids.get(k + "_pid"), pids.get(k + "_ident")
        if pid and not ident:
            return True  # recorded without an identity: cannot prove it is gone
        if proc_alive(pid, ident):
            return True
    return False


def _record(sid):
    rec = read_json(path("sessions", sid + ".json"), None)
    return rec if isinstance(rec, dict) else {}


def _prunable_session(sid, cutoff):
    if _last_change(_session_paths(sid)) >= cutoff:  # GUARD:prune-age
        return False
    rec = _record(sid)
    if rec.get("ended"):
        return True
    known = rec.get("pid") and rec.get("pid_ident")
    if not known and os.path.exists(path("sessions", sid + ".json")):  # GUARD:prune-unknown
        return False  # its process (or its identity) was never recorded: cannot prove it is gone
    return not proc_alive(rec.get("pid"), rec.get("pid_ident"))  # GUARD:prune-live


_TEMP = re.compile(r"\.tmp\.\d+\.[0-9a-f]{8}$")  # common.atomic_write_json's temp names


def _stale_temp_files(cutoff):
    out = []
    for box in ("sessions", "outbox", "runs"):
        try:
            names = os.listdir(path(box))
        except OSError:
            continue
        for n in names:
            p = path(box, n)
            if _TEMP.search(n) and _last_change([p]) < cutoff:
                out.append(p)
    return out


def _remove_session(sid):
    return all([_remove(p) for p in _session_paths(sid)])


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
        sid = meta.get("session_id") if isinstance(meta, dict) else None
        if isinstance(sid, str):
            run_of[sid] = name
    doomed_runs = set()
    if runs:
        for name in run_names:
            d = path("runs", name)
            if _last_change([d]) < cutoff and not _run_alive(d):
                doomed_runs.add(name)
    with_run = {}  # run name -> its prunable sessions; they go only with the run
    for sid in sorted(_session_ids()):
        if limit is not None and len(out["sessions"]) >= limit:
            break
        if not _prunable_session(sid, cutoff):
            continue
        runs_of_sid = [r for r in (_record(sid).get("cxd_run"), run_of.get(sid)) if r in run_names]
        if runs_of_sid:
            if all(r in doomed_runs for r in runs_of_sid):
                with_run.setdefault(runs_of_sid[0], []).append(sid)
            continue  # stays while any of its runs stays
        if dry_run:
            out["sessions"].append(sid)
        elif _prunable_session(sid, cutoff) and _remove_session(sid):  # re-check: a resume may have touched it
            out["sessions"].append(sid)
    for name in sorted(doomed_runs):
        d = path("runs", name)
        if dry_run:
            out["runs"].append(name)
            out["sessions"] += with_run.get(name, [])
            continue
        if _last_change([d]) >= cutoff or _run_alive(d):  # GUARD:prune-run-recheck
            continue  # resumed meanwhile: the run and its sessions stay
        if _remove(d):
            out["runs"].append(name)
            out["sessions"] += [sid for sid in with_run.get(name, [])
                                if _prunable_session(sid, cutoff) and _remove_session(sid)]
    if not dry_run:
        for p in _stale_temp_files(cutoff):
            _remove(p)
    return out


def auto_prune_due():
    """At most once an hour (a stamp in the future, from a clock that stepped
    back, counts as due). Claims the slot by touching the stamp."""
    stamp = path("prune.stamp")
    try:
        age = time.time() - os.stat(stamp).st_mtime
        if 0 <= age < AUTO_EVERY:
            return False
    except OSError:
        pass
    try:
        with open(stamp, "a"):
            pass
        os.utime(stamp, None)
    except OSError:
        return False
    return True


def spawn_auto_prune(cxd_bin, python):
    """From the SessionStart hook: prune in a detached process, so the session
    never waits for it."""
    import subprocess
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        subprocess.Popen([python, cxd_bin, "_prune-auto"], stdin=devnull, stdout=devnull, stderr=devnull,
                         close_fds=True, start_new_session=True)
    finally:
        os.close(devnull)
