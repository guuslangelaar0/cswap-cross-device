"""The claim store (docs/design/broker.md, Delivery): a request id runs at
most once on this machine, whichever path and attempt delivers it.

Every process on the machine that executes requests shares it: `cxd _rpc`
started over ssh, and the broker agent's `cxd _rpc` children. Each id has a
record, ~/.cxd/dedupe/<id>.json, and a lock, <id>.lock. Every change holds
the lock, reads the record, checks that it is in the state the change
expects, and replaces it atomically:

  (none)    -> awaiting   the first attempt arrives; it owns the claim
  awaiting  -> awaiting   an attempt with a higher sequence number takes it over
  awaiting  -> running    go from the owner, before the lapse deadline
  awaiting  -> cancelled  the deadline passed without go: nothing ran
  running   -> done/error the executor finished
  running   -> unknown    the executor died before it finished

Every operation also holds the store lock (.store.lock) shared; prune holds
it exclusively, so a lock file is only removed while nobody holds or waits
on it.

The lapse deadline is the record's wall time: a clock step makes a claim
lapse early (cancelled: the client retries with a new id) or late (harmless:
a client sends go only while it waits).
"""

import fcntl
import json
import os
import re
import math
import time

from .common import atomic_write, ensure_dir, path, proc_identity, proc_state

AWAITING, RUNNING, DONE, ERROR, CANCELLED, UNKNOWN = (
    "awaiting", "running", "done", "error", "cancelled", "unknown")
FINAL = (DONE, ERROR, CANCELLED, UNKNOWN)
LAPSE_SLACK = 5.0
KEEP_FINAL_SECONDS = 24 * 3600
KEEP_BY_ID_SECONDS = 36 * 3600
MAX_WAIT = 12 * 3600  # no client waits longer (the CLI caps its timeouts at 12 h)
_ID_RE = re.compile(r"[0-9a-f]{32}")


class Mismatch(ValueError):
    """An attempt whose operation or arguments differ from the claim's."""


def new_request_id(now=None):
    """32 hex digits: the creation time in milliseconds (48 bits), then 80
    random bits."""
    ms = int((time.time() if now is None else now) * 1000) & ((1 << 48) - 1)
    return "%012x%s" % (ms, os.urandom(10).hex())


def id_time(rid):
    """The creation time an id carries (seconds)."""
    return int(rid[:12], 16) / 1000.0


def _check(*ids):
    for i in ids:
        if not isinstance(i, str) or not _ID_RE.fullmatch(i):  # GUARD:claim-id
            raise ValueError("a request or attempt id is 32 lowercase hex digits")


def _files(rid):
    d = ensure_dir(path("dedupe"))
    return os.path.join(d, rid + ".json"), os.path.join(d, rid + ".lock")


def _store_lock(mode):
    fd = os.open(os.path.join(ensure_dir(path("dedupe")), ".store.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sync_dir(d):
    """Make a new or replaced name in d survive a crash of the host."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unreadable(p, why):
    try:
        changed = os.stat(p).st_mtime
    except OSError:
        changed = 0
    return {"state": UNKNOWN, "unreadable": True, "changed": changed,
            "error": {"code": "outcome_unknown", "message": "unreadable claim (%s)" % why}}


class _Record(object):
    """A claim's record, read and written by whoever holds its locks."""

    def __init__(self, rid):
        self.rec_path, self.lock_path = _files(rid)

    def read(self):
        """The record; for a file that is there but no claim (empty after a
        crash, corrupt, the wrong shape) an `unknown` record marked unreadable,
        whose outcome nobody can know."""
        try:
            with open(self.rec_path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            return _unreadable(self.rec_path, type(e).__name__)
        if not isinstance(rec, dict) or rec.get("state") not in (AWAITING, RUNNING) + FINAL:  # GUARD:claim-shape
            return _unreadable(self.rec_path, "not a claim")
        return rec

    def create(self, rec):
        """The first record; os.link fails if one exists, so even a caller
        that forgot the lock could not overwrite a claim. Synced to disk: a
        claim lost in a crash could run its request a second time."""
        tmp = "%s.new.%d.%s" % (self.rec_path, os.getpid(), os.urandom(4).hex())
        atomic_write(tmp, _dump(rec), fsync=True)
        try:
            os.link(tmp, self.rec_path)
            return True
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)
            _sync_dir(os.path.dirname(self.rec_path))

    def write(self, rec):
        atomic_write(self.rec_path, _dump(rec), fsync=True)  # GUARD:claim-durable
        _sync_dir(os.path.dirname(self.rec_path))


class _Locked(_Record):
    """The claim's lock (and the store lock, shared), held for one
    read-check-write."""

    def __enter__(self):
        self.store = _store_lock(fcntl.LOCK_SH)
        try:
            self.fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        except BaseException:
            os.close(self.store)
            raise
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)  # GUARD:claim-lock
        except BaseException:
            os.close(self.fd)
            os.close(self.store)
            raise
        return self

    def __exit__(self, *exc):
        os.close(self.fd)  # releases the locks
        os.close(self.store)


def _dump(rec):
    return json.dumps(rec, sort_keys=True).encode("utf-8")


def _settle(lk, rec, now):
    """Apply the changes that time and death make: an awaiting claim past its
    deadline is cancelled; a running claim whose executor died is unknown."""
    if rec is None or rec.get("unreadable"):
        return rec
    if rec.get("state") == AWAITING and now >= rec.get("lapse_at", 0):  # GUARD:claim-lapse
        rec.update(state=CANCELLED, changed=now)
        lk.write(rec)
    elif rec.get("state") == RUNNING:
        ex = rec.get("executor") or {}
        if proc_state(ex.get("pid"), ex.get("ident")) == "dead":  # GUARD:claim-executor-dead
            rec.update(state=UNKNOWN, changed=now,
                       error={"code": "outcome_unknown", "message": "the executor died before it finished"})
            lk.write(rec)
    return rec


def arrive(rid, attempt, wait, seq=0, op=None, digest=None, now=None):
    """Attempt number `seq` of rid, for operation `op` with arguments whose
    digest is `digest`, arrives with a client that will wait `wait` more
    seconds. It takes over an awaiting claim only with a higher sequence
    number than the owner's (a stale attempt never does). An attempt for
    another operation or other arguments raises Mismatch (a --request-id
    given to the wrong command must not get another command's outcome).
    Returns the record as it stands afterwards."""
    _check(rid, attempt)
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ValueError("an attempt's sequence number is an integer")
    try:
        wait = float(wait)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("wait is a number of seconds")
    if not math.isfinite(wait):  # GUARD:claim-wait-finite (inf would never lapse nor be pruned)
        raise ValueError("wait is a finite number of seconds")
    now = time.time() if now is None else now
    deadline = now + min(max(0.0, wait), MAX_WAIT) + LAPSE_SLACK
    with _Locked(rid) as lk:
        rec = _settle(lk, lk.read(), now)
        if rec is not None and rec.get("unreadable"):
            return rec  # its outcome is unknown, whatever operation asks
        if rec is None:
            rec = {"id": rid, "state": AWAITING, "owner": attempt, "seq": seq, "lapse_at": deadline,
                   "created": now, "changed": now, "op": op, "digest": digest}
            if not lk.create(rec):  # cannot happen under the lock; never overwrite
                rec = _settle(lk, lk.read(), now)
        if (rec.get("op"), rec.get("digest")) != (op, digest):  # GUARD:claim-same-op
            raise Mismatch("request %s was for another operation or other arguments" % rid)
        if rec["state"] == AWAITING and seq > rec.get("seq", 0):  # GUARD:claim-seq
            rec.update(owner=attempt, seq=seq, lapse_at=max(rec["lapse_at"], deadline), changed=now)
            lk.write(rec)
        return rec


def go(rid, attempt, now=None):
    """The attempt's client still waits: run it if this attempt owns the claim.
    Returns (verdict, record): "run" (the caller executes, then finish()),
    "duplicate" (another attempt owns or runs it: wait_final()), "cancelled"
    (the claim lapsed: nothing ran), "refused" (this process cannot record
    itself as the executor: nothing ran) or "final" (the record has the
    outcome)."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    if not os.path.exists(_files(rid)[0]):
        raise ValueError("go for a request that never arrived")  # (and no lock file left behind)
    with _Locked(rid) as lk:
        rec = _settle(lk, lk.read(), now)
        if rec is None:
            raise ValueError("go for a request that never arrived")
        state = rec["state"]
        if state == AWAITING:
            if rec.get("owner") != attempt:  # GUARD:claim-go-owner
                return "duplicate", rec
            pid = os.getpid()
            ident = proc_identity(pid)
            if not ident:  # GUARD:claim-executor-ident
                # Without its identity nobody could ever tell that the executor
                # died: the claim would stay running for good. Do not run; the
                # claim stays awaiting and lapses (nothing ran).
                return "refused", rec
            rec.update(state=RUNNING, changed=now, executor={"pid": pid, "ident": ident})
            lk.write(rec)
            return "run", rec
        if state == RUNNING:
            return "duplicate", rec
        return ("cancelled" if state == CANCELLED else "final"), rec


def cancel(rid, attempt, now=None):
    """The attempt's client went away before go: cancel the claim if this
    attempt still owns it and it is awaiting (a newer attempt that took it
    over is left alone). True when it cancelled."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    if not os.path.exists(_files(rid)[0]):
        return False
    with _Locked(rid) as lk:
        rec = _settle(lk, lk.read(), now)
        if not rec or rec.get("state") != AWAITING or rec.get("owner") != attempt:  # GUARD:claim-cancel-owner
            return False
        rec.update(state=CANCELLED, changed=now)
        lk.write(rec)
        return True


def finish(rid, attempt, reply=None, error=None, now=None):
    """The executor records the outcome. False when this attempt is not the
    claim's running owner (the record is left alone)."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    if not os.path.exists(_files(rid)[0]):
        return False
    with _Locked(rid) as lk:
        rec = lk.read()
        if not rec or rec.get("state") != RUNNING or rec.get("owner") != attempt:  # GUARD:claim-finish-running
            return False
        if error is not None:
            rec.update(state=ERROR, error=error)
        else:
            rec.update(state=DONE, reply=reply)
        rec["changed"] = now
        lk.write(rec)
        return True


def current(rid, now=None):
    """The record with time and death applied, or None."""
    _check(rid)
    now = time.time() if now is None else now
    if not os.path.exists(_files(rid)[0]):
        return None  # a lookup leaves no lock file behind
    with _Locked(rid) as lk:
        return _settle(lk, lk.read(), now)


def wait_final(rid, timeout, poll=0.2):
    """Wait up to timeout seconds for a final record; None if none came."""
    end = time.monotonic() + timeout
    while True:
        rec = current(rid)
        if rec is None or rec.get("state") in FINAL:
            return rec
        if time.monotonic() >= end:
            return None
        time.sleep(min(poll, max(0.0, end - time.monotonic())))


def prune(now=None):
    """Remove records that reached a final state KEEP_FINAL_SECONDS ago and
    whose id was made KEEP_BY_ID_SECONDS ago (so a clock that jumped forward
    cannot prune a record early on its own); also lock files without a record
    and temporary files an hour old. A store busy with claims is left for the
    next pass. Returns the removed ids."""
    now = time.time() if now is None else now
    d = path("dedupe")
    if not os.path.isdir(d):
        return []
    removed = []
    try:
        store = _store_lock(fcntl.LOCK_EX | fcntl.LOCK_NB)  # GUARD:claim-store-lock
    except BlockingIOError:
        return []  # claims are being made or changed: next pass
    try:
        names = os.listdir(d)
        for n in sorted(names):
            p = os.path.join(d, n)
            if n.endswith(".json") and _ID_RE.fullmatch(n[:-5]):
                rid = n[:-5]
                try:
                    lk = _Record(rid)  # under the exclusive store lock nobody holds a claim lock
                    rec = _settle(lk, lk.read(), now)
                    if not rec or rec.get("state") not in FINAL or now - float(rec.get("changed", now)) < KEEP_FINAL_SECONDS:
                        continue
                    if now - id_time(rid) < KEEP_BY_ID_SECONDS:  # GUARD:claim-prune-id-time
                        continue
                    for q in (lk.rec_path, lk.lock_path):
                        try:
                            os.unlink(q)
                        except FileNotFoundError:
                            pass
                    removed.append(rid)
                except (OSError, ValueError, TypeError):
                    continue  # one bad record must not stop the pass
            elif n.endswith(".lock") and n != ".store.lock" and not os.path.exists(p[:-5] + ".json"):
                _remove_quietly(p)  # a lock without a record (a go for an unknown id, a crash)
            elif (".new." in n or ".tmp." in n) and _older_than(p, now, 3600):
                _remove_quietly(p)  # left by a process killed mid-write
    finally:
        os.close(store)
    return removed


def _older_than(p, now, seconds):
    try:
        return now - os.stat(p).st_mtime > seconds
    except OSError:
        return False


def _remove_quietly(p):
    try:
        os.unlink(p)
    except OSError:
        pass
