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

from .common import CxdError, path, proc_state, read_json

DEFAULT_DAYS = 7.0
AUTO_EVERY = 3600.0
AUTO_LIMIT = 200
_SESSION_PARTS = (("sessions", "%s.json"), ("outbox", "%s.jsonl"), ("inbox", "%s"),
                  ("claimed", "%s"), ("delivered", "%s"))


NEVER = float("inf")


def _valid_days(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return v > 0 and not math.isnan(v)
    except OverflowError:
        return False


def age_days(cfg, override=None):
    """The age to prune at. A bad override is an error; a bad config value
    means the default (a broken config never breaks anything)."""
    if override is not None:
        if not _valid_days(override) or not math.isfinite(override):
            raise CxdError("--older-than must be a positive number of days, got %r" % override, "usage")
        return _days(override)
    v = (cfg or {}).get("prune_after_days")
    return _days(v) if _valid_days(v) else DEFAULT_DAYS


def _days(v):
    """A huge age (or infinity) means: never prune."""
    try:
        f = float(v)
    except OverflowError:
        return NEVER
    return NEVER if f * 86400 > 1e12 else f


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
    """False only when the run is provably over: every recorded process is
    provably gone. A run without pids.json counts as over only if its start
    failed (an exit record); anything that cannot be checked counts as alive."""
    pids = read_json(os.path.join(d, "pids.json"), None)
    if pids is None:
        return not os.path.exists(os.path.join(d, "exit.json"))  # GUARD:prune-run-unknown
    if not isinstance(pids, dict):
        return True
    for k in ("claude", "keeper", "supervisor"):
        pid, ident = pids.get(k + "_pid"), pids.get(k + "_ident")
        if pid and proc_state(pid, ident) != "dead":
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
    return proc_state(rec.get("pid"), rec.get("pid_ident")) == "dead"  # GUARD:prune-live


_TEMP = re.compile(r"\.tmp\.\d+\.[0-9a-f]{8}$")  # common.atomic_write_json's temp names


def _stale_temp_files(cutoff):
    out = []
    for box in ("sessions", "outbox"):  # not runs/: a run may have any name
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
    cutoff = 0.0 if days == NEVER else time.time() - days * 86400
    out = {"older_than_days": days, "sessions": [], "runs": [], "dry_run": bool(dry_run)}
    try:
        run_names = sorted(n for n in os.listdir(path("runs")) if os.path.isdir(path("runs", n)))
    except FileNotFoundError:
        run_names = []
    # (Any other error listing runs/ propagates: without the runs, their
    # sessions could not be protected.)
    run_of = {}  # session id -> the runs that name it (every gen's session id)
    for name in run_names:
        meta = read_json(path("runs", name, "meta.json"), {})
        if not isinstance(meta, dict):
            continue
        ids = meta.get("session_ids") if isinstance(meta.get("session_ids"), list) else []
        for sid in ids + [meta.get("session_id")]:
            if isinstance(sid, str):
                run_of.setdefault(sid, set()).add(name)
    doomed_runs = set()
    if runs:
        for name in run_names:
            d = path("runs", name)
            if _last_change([d]) < cutoff and not _run_alive(d):
                doomed_runs.add(name)
    with_runs = {}  # session id -> its runs; it goes only once all of them are gone
    for sid in sorted(_session_ids()):
        if limit is not None and len(out["sessions"]) >= limit:
            break
        if not _prunable_session(sid, cutoff):
            continue
        rec_run = _record(sid).get("cxd_run")
        runs_of_sid = set(r for r in run_of.get(sid, ())) | ({rec_run} if isinstance(rec_run, str) else set())
        runs_of_sid &= set(run_names)
        if runs_of_sid:
            if runs_of_sid <= doomed_runs:
                with_runs[sid] = runs_of_sid
            continue  # stays while any of its runs stays
        if dry_run or _remove_session(sid):
            out["sessions"].append(sid)
    removed_runs = set()
    for name in sorted(doomed_runs):
        d = path("runs", name)
        if not dry_run:
            if _last_change([d]) >= cutoff or _run_alive(d):  # GUARD:prune-run-recheck
                continue  # resumed meanwhile: the run and its sessions stay
            if not _remove(d):
                continue
        removed_runs.add(name)
        out["runs"].append(name)
    for sid, its_runs in sorted(with_runs.items()):
        if its_runs <= removed_runs and (dry_run or (_prunable_session(sid, cutoff) and _remove_session(sid))):
            out["sessions"].append(sid)
    if not dry_run:
        for p in _stale_temp_files(cutoff):
            _remove(p)
    return out


def auto_prune_due(cfg=None):
    """At most once an hour (a stamp in the future, from a clock that stepped
    back, counts as due), and never with "auto_prune": false. Claims the slot
    by touching the stamp."""
    if (cfg or {}).get("auto_prune") is False:
        return False
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
        subprocess.Popen([python, "-I", "-S", cxd_bin, "_prune-auto"], stdin=devnull, stdout=devnull,
                         stderr=devnull, close_fds=True, start_new_session=True, cwd="/")
    finally:
        os.close(devnull)


def auto_pass(cfg):
    """`cxd _prune-auto`: one pass at a time (concurrent SessionStarts may each
    start one)."""
    import fcntl
    with open(path("prune.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        return prune(cfg, limit=AUTO_LIMIT)
