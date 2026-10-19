"""The supervisor and the keeper of a headless run.

supervisor (one per gen, detached, own session):
    mkfifo in.fifo; open its read end non-blocking; start the keeper, which
    opens the write end and holds it; hand the read end to `cswap run <acct>
    -- -p --input-format stream-json ...` (or plain `claude -p ...` when the
    run has no account; plus --dangerously-skip-permissions only when the
    launcher passed --skip-permissions) as stdin; record pids; wait for the
    claude process; write exit.json; reap the keeper.

keeper: opens the FIFO for writing and sleeps. It is the ONLY long-lived
writer. While it lives, claude's stdin never reaches EOF between messages;
when it dies (cxd stop), claude sees EOF and ends the session.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from .common import (
    append_jsonl, atomic_write_json, cxd_bin, find_claude, find_cswap, now_iso, proc_identity,
    proc_parent_and_args, read_json, worker_env,
)
from .state import CLAUDE_HEADLESS_ARGS, SKIP_PERMISSIONS_ARGS


def keeper_main(fifo, ready):
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fd = os.open(fifo, os.O_WRONLY)  # blocks until the supervisor holds the read end
    atomic_write_json(ready, {"pid": os.getpid(), "ts": now_iso()})
    while True:
        signal.pause()
    os.close(fd)  # pragma: no cover


def _start_keeper(d, fifo):
    ready = os.path.join(d, "keeper.ready")
    try:
        os.unlink(ready)
    except OSError:
        pass
    keeper = subprocess.Popen(  # GUARD:keeper
        [sys.executable, cxd_bin(), "_keeper", fifo, ready],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(ready):
            return keeper
        if keeper.poll() is not None:
            break
        time.sleep(0.02)
    keeper.kill()
    keeper.wait()
    raise RuntimeError("keeper failed to open the FIFO")


def supervise_main(d, gen, resume_sid=None, skip_permissions=False):
    """Run one gen. Whatever goes wrong, never leave the gen without an exit
    record (watch would wait for it forever) or with an orphaned keeper (it
    would hold the FIFO open)."""
    try:
        return _supervise(d, gen, resume_sid, skip_permissions)
    except BaseException as e:  # GUARD:supervise-record
        _record_unexpected_exit(d, gen, e)
        raise


def _record_unexpected_exit(d, gen, e):
    try:
        p = os.path.join(d, "exit.json")
        if (read_json(p, None) or {}).get("gen") != gen:
            atomic_write_json(p, {"gen": gen, "rc": None, "ended": now_iso(),
                                  "error": "supervisor failed: %s: %s" % (type(e).__name__, e)})
    finally:
        # The keeper is our own child; it wrote its pid to keeper.ready.
        pid = (read_json(os.path.join(d, "keeper.ready"), {}) or {}).get("pid")
        if pid and proc_parent_and_args(pid)[0] == os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass


def _supervise(d, gen, resume_sid, skip_permissions):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    meta = read_json(os.path.join(d, "meta.json"), {}) or {}
    fifo = os.path.join(d, "in.fifo")
    out_path = os.path.join(d, "out.jsonl")

    def fail(msg):
        atomic_write_json(os.path.join(d, "exit.json"),
                          {"gen": gen, "rc": None, "error": msg, "ended": now_iso()})
        return 1

    for stale in (fifo, fifo + ".lock"):  # a new gen: a writer still waiting on the old lock must not block it
        try:
            os.unlink(stale)
        except OSError:
            pass
    os.mkfifo(fifo, 0o600)
    rfd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        keeper = _start_keeper(d, fifo)
    except Exception as e:
        os.close(rfd)
        return fail(str(e))
    fl = fcntl.fcntl(rfd, fcntl.F_GETFL)
    fcntl.fcntl(rfd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)

    account = meta.get("account")
    if account:
        launcher = find_cswap()
        prefix = [launcher, "run", account, "--"]
    else:
        launcher = find_claude()
        prefix = [launcher]
    if not launcher:
        os.close(rfd)
        keeper.terminate()
        keeper.wait()
        return fail("%s not found" % ("cswap" if account else "claude"))
    cmd = prefix + CLAUDE_HEADLESS_ARGS
    if skip_permissions:
        cmd += SKIP_PERMISSIONS_ARGS
    if resume_sid:
        cmd += ["--resume", resume_sid]
    cmd += list(meta.get("claude_args") or [])

    append_jsonl(out_path, {"type": "cxd", "event": "gen-start", "gen": gen, "ts": now_iso(),
                            "resume": resume_sid})
    out = os.open(out_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    err = os.open(os.path.join(d, "err.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        claude = subprocess.Popen(cmd, stdin=rfd, stdout=out, stderr=err, close_fds=True,
                                  cwd=meta.get("cwd") or os.path.expanduser("~"),
                                  env=worker_env({"CXD_RUN": meta.get("name", "")}))
    except OSError as e:
        keeper.terminate()
        keeper.wait()
        return fail("spawn failed: %s" % e)
    finally:
        os.close(rfd)  # the supervisor never holds the FIFO
        os.close(out)
        os.close(err)

    pids = {
        "gen": gen, "started": now_iso(),
        "supervisor_pid": os.getpid(), "supervisor_ident": proc_identity(os.getpid()),
        "keeper_pid": keeper.pid, "keeper_ident": proc_identity(keeper.pid),
        "claude_pid": claude.pid, "claude_ident": proc_identity(claude.pid),
        "cmd": cmd[:4] + ["..."], "skip_permissions": bool(skip_permissions),
    }
    atomic_write_json(os.path.join(d, "pids.gen%d.json" % gen), pids)  # history, for audits/cleanup
    atomic_write_json(os.path.join(d, "pids.json"), pids)
    while True:
        try:
            rc = claude.wait()
            break
        except InterruptedError:  # pragma: no cover
            continue
    atomic_write_json(os.path.join(d, "exit.json"), {"gen": gen, "rc": rc, "ended": now_iso()})
    append_jsonl(out_path, {"type": "cxd", "event": "gen-exit", "gen": gen, "rc": rc, "ts": now_iso()})
    if keeper.poll() is None:
        keeper.terminate()
        try:
            keeper.wait(5)
        except subprocess.TimeoutExpired:
            keeper.kill()
            keeper.wait()
    return 0
