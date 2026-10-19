"""The claim store (docs/design/broker.md, Delivery): a request id runs at
most once on this machine, whichever path and attempt delivers it.

Every process on the machine that executes requests shares it: `cxd _rpc`
started over ssh, and the broker agent's `cxd _rpc` children. Each id has a
record, ~/.cxd/dedupe/<id>.json, and a lock, <id>.lock. Every change holds
the lock, reads the record, checks that it is in the state the change
expects, and replaces it atomically:

  (none)    -> awaiting   the first attempt arrives; it owns the claim
  awaiting  -> awaiting   a later attempt with a waiting client takes it over
  awaiting  -> running    go from the owner, before the lapse deadline
  awaiting  -> cancelled  the deadline passed without go: nothing ran
  running   -> done/error the executor finished
  running   -> unknown    the executor died before it finished

The lapse deadline is the record's wall time: a clock step makes a claim
lapse early (cancelled: the client retries with a new id) or late (harmless:
a client sends go only while it waits).
"""

import fcntl
import json
import os
import re
import time

from .common import atomic_write, ensure_dir, path, proc_identity, proc_state

AWAITING, RUNNING, DONE, ERROR, CANCELLED, UNKNOWN = (
    "awaiting", "running", "done", "error", "cancelled", "unknown")
FINAL = (DONE, ERROR, CANCELLED, UNKNOWN)
LAPSE_SLACK = 5.0
KEEP_FINAL_SECONDS = 24 * 3600
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _check(*ids):
    for i in ids:
        if not isinstance(i, str) or not _ID_RE.match(i):  # GUARD:claim-id
            raise ValueError("a request or attempt id is 32 lowercase hex digits")


def _files(rid):
    d = ensure_dir(path("dedupe"))
    return os.path.join(d, rid + ".json"), os.path.join(d, rid + ".lock")


class _Locked(object):
    """The claim's lock, held for one read-check-write."""

    def __init__(self, rid):
        self.rec_path, lock_path = _files(rid)
        self.fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)

    def __enter__(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX)  # GUARD:claim-lock
        return self

    def __exit__(self, *exc):
        os.close(self.fd)  # releases the lock

    def read(self):
        try:
            with open(self.rec_path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except FileNotFoundError:
            return None
        except ValueError:
            rec = {"state": UNKNOWN, "error": {"code": "outcome_unknown", "message": "unreadable claim"}}
        return rec if isinstance(rec, dict) else None

    def create(self, rec):
        """The first record; os.link fails if one exists, so even a caller
        that forgot the lock could not overwrite a claim."""
        tmp = "%s.new.%d.%s" % (self.rec_path, os.getpid(), os.urandom(4).hex())
        atomic_write(tmp, _dump(rec))
        try:
            os.link(tmp, self.rec_path)
            return True
        except FileExistsError:
            return False
        finally:
            os.unlink(tmp)

    def write(self, rec):
        atomic_write(self.rec_path, _dump(rec))


def _dump(rec):
    return json.dumps(rec, sort_keys=True).encode("utf-8")


def _settle(lk, rec, now):
    """Apply the changes that time and death make: an awaiting claim past its
    deadline is cancelled; a running claim whose executor died is unknown."""
    if rec is None:
        return None
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


def arrive(rid, attempt, wait, now=None):
    """An attempt of rid arrives with a client that will wait `wait` more
    seconds. Returns the record as it stands afterwards."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    deadline = now + max(0.0, float(wait)) + LAPSE_SLACK
    with _Locked(rid) as lk:
        rec = _settle(lk, lk.read(), now)
        if rec is None:
            rec = {"id": rid, "state": AWAITING, "owner": attempt, "lapse_at": deadline,
                   "created": now, "changed": now}
            if not lk.create(rec):  # cannot happen under the lock; never overwrite
                rec = _settle(lk, lk.read(), now)
        elif rec["state"] == AWAITING:
            rec.update(owner=attempt, lapse_at=max(rec["lapse_at"], deadline), changed=now)
            lk.write(rec)
        return rec


def go(rid, attempt, now=None):
    """The attempt's client still waits: run it if this attempt owns the claim.
    Returns (verdict, record): "run" (the caller executes, then finish()),
    "duplicate" (another attempt owns or runs it: wait_final()), "cancelled"
    (the claim lapsed: nothing ran) or "final" (the record has the outcome)."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    with _Locked(rid) as lk:
        rec = _settle(lk, lk.read(), now)
        if rec is None:
            raise ValueError("go for a request that never arrived")
        state = rec["state"]
        if state == AWAITING:
            if rec.get("owner") != attempt:  # GUARD:claim-go-owner
                return "duplicate", rec
            pid = os.getpid()
            rec.update(state=RUNNING, changed=now, executor={"pid": pid, "ident": proc_identity(pid)})
            lk.write(rec)
            return "run", rec
        if state == RUNNING:
            return "duplicate", rec
        return ("cancelled" if state == CANCELLED else "final"), rec


def finish(rid, attempt, reply=None, error=None, now=None):
    """The executor records the outcome. False when this attempt is not the
    claim's running owner (the record is left alone)."""
    _check(rid, attempt)
    now = time.time() if now is None else now
    with _Locked(rid) as lk:
        rec = lk.read()
        if not rec or rec.get("state") != RUNNING or rec.get("owner") != attempt:
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
    """Remove records that reached a final state KEEP_FINAL_SECONDS ago.
    Returns the removed ids."""
    now = time.time() if now is None else now
    d = path("dedupe")
    try:
        names = os.listdir(d)
    except OSError:
        return []
    removed = []
    for n in sorted(names):
        if not n.endswith(".json") or not _ID_RE.match(n[:-5]):
            continue
        rid = n[:-5]
        with _Locked(rid) as lk:
            rec = _settle(lk, lk.read(), now)
            if not rec or rec.get("state") not in FINAL or now - rec.get("changed", now) < KEEP_FINAL_SECONDS:
                continue
            os.unlink(lk.rec_path)
            os.unlink(os.path.join(d, rid + ".lock"))
            removed.append(rid)
    return removed
