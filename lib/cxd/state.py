"""Host-side operations. Every function here runs ON the host that owns the
run or session; transports only carry the request and the reply.

Layout under ~/.cxd (mode 700 throughout):

    runs/<name>/        a headless run started by `cxd start`
        meta.json       name, account, bypass_opt_in (the run's own opt-in to the
                        permission bypass), cwd, claude_args, gen, session_id
        pids.json       supervisor/keeper/claude pids + start identities (current gen)
        exit.json       rc of the current gen, written by the supervisor
        in.fifo         stdin of the claude process; the keeper holds the write end
        out.jsonl       stream-json output (all gens, separated by cxd gen-start lines)
        err.log         stderr
    sessions/<sid>.json registered by the SessionStart hook (any session, any account)
    inbox/<sid>/*.json  messages waiting for a hook to deliver them
    claimed/<sid>/      messages a hook has claimed and not yet handed over
                        (<id>.claim-<pid>-<hash>.json; requeued if that hook died)
    delivered/<sid>/    messages a hook has handed over (moved here once acked)
    outbox/<sid>.jsonl  Stop reports, `cxd notify` lines, delivery receipts
"""

import errno
import fcntl
import json
import os
import random
import re
import signal
import subprocess
import sys
import time

from .common import (
    CxdError, NAME_RE, SID_RE, VERSION, PROTO, append_jsonl, atomic_write_json,
    cxd_bin, cxd_home, ensure_dir, excerpt, find_claude, find_cswap, load_config,
    now_iso, path, proc_alive, read_json, self_label, signal_verified, wait_dead,
    worker_env,
)

PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

CLAUDE_HEADLESS_ARGS = [
    "-p", "--input-format", "stream-json", "--output-format", "stream-json",
    "--verbose", "--replay-user-messages",
]
# Added only on an explicit opt-in, see bypass_opted_in().
SKIP_PERMISSIONS_ARGS = ["--dangerously-skip-permissions"]


def bypass_opted_in(run_opt_in):
    """Permission prompts are bypassed only on an explicit opt-in, and only a
    JSON true counts: the run's own (`--skip-permissions` on start or resume,
    kept as bypass_opt_in in meta.json) or "skip_permissions" in this host's
    config.json, read at every launch so that switching it off takes effect.
    (cxd 0.1.0 wrote "skip_permissions" into meta.json mixed with the host
    setting; that key is ignored.)"""
    return run_opt_in is True or load_config().get("skip_permissions") is True  # GUARD:bypass-json-true


# ================================================================ runs

def run_dir(name):
    if not NAME_RE.match(name or ""):
        raise CxdError("invalid run name %r (letters, digits, . _ -; max 64)" % name, "usage")
    return path("runs", name)


def _load_run(name):
    d = run_dir(name)
    if not os.path.isdir(d):
        raise CxdError("no run named %r on %s" % (name, self_label()), "not_found")
    return d


def _run_files(d):
    return (read_json(os.path.join(d, "meta.json"), {}) or {},
            read_json(os.path.join(d, "pids.json"), {}) or {},
            read_json(os.path.join(d, "exit.json"), None))


def claude_alive(pids):
    return proc_alive(pids.get("claude_pid"), pids.get("claude_ident"))


def keeper_alive(pids):
    return proc_alive(pids.get("keeper_pid"), pids.get("keeper_ident"))


def supervisor_alive(pids):
    return proc_alive(pids.get("supervisor_pid"), pids.get("supervisor_ident"))


def _run_accepts_input(d):
    """A run takes input only while its claude process AND its keeper live."""
    _, pids, _ = _run_files(d)
    return claude_alive(pids) and keeper_alive(pids)


def fifo_write(fifo, data):
    """Write one complete message to a FIFO. Raises CxdError('dead') if no
    process is reading it. Never holds the FIFO open after returning."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)  # GUARD:fifo-open
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            raise CxdError("no process is reading %s: the run is not running" % fifo, "dead")
        raise
    try:
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    except BrokenPipeError:
        raise CxdError("the run's reader went away while writing", "dead")
    finally:
        os.close(fd)


def user_message(text):
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode("utf-8")


def _spawn_supervisor(d, gen, resume_sid, skip_permissions):
    log = os.open(os.path.join(d, "supervisor.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    args = [sys.executable, cxd_bin(), "_supervise", d, "--gen", str(gen)]
    if resume_sid:
        args += ["--resume", resume_sid]
    if skip_permissions:
        args += ["--skip-permissions"]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         close_fds=True, start_new_session=True, env=worker_env())
    finally:
        os.close(log)


def _wait_pids(d, gen, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pids = read_json(os.path.join(d, "pids.json"), {}) or {}
        if pids.get("gen") == gen and pids.get("claude_pid"):
            return pids
        ex = read_json(os.path.join(d, "exit.json"), None)
        if ex and ex.get("gen") == gen:
            raise CxdError("run failed to start: %s" % (ex.get("error") or "rc=%s" % ex.get("rc")), "start_failed")
        time.sleep(0.05)
    raise CxdError("supervisor did not report pids within %.0fs (see %s/supervisor.log)" % (timeout, d), "start_failed")


def _wait_session_id(d, gen, timeout):
    deadline = time.time() + timeout
    out = os.path.join(d, "out.jsonl")
    while True:
        sid = _scan_session_id(out, gen)
        if sid:
            return sid
        _, pids, _ = _run_files(d)
        if not claude_alive(pids) or time.time() >= deadline:
            return None
        time.sleep(0.2)


def _scan_session_id(out, gen=None):
    sid = None
    cur_gen = 0
    try:
        with open(out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                ev = _parse(line)
                if not ev:
                    continue
                if ev.get("type") == "cxd" and ev.get("event") == "gen-start":
                    cur_gen = ev.get("gen", cur_gen)
                elif ev.get("type") == "system" and ev.get("subtype") == "init":
                    if gen is None or cur_gen == gen:
                        sid = ev.get("session_id") or sid
    except OSError:
        pass
    return sid


def _parse(line):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        v = json.loads(line)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def op_start(args):
    name = args.get("name")
    prompt = args.get("prompt")
    if not prompt:
        raise CxdError("empty prompt", "usage")
    d = run_dir(name)
    if os.path.exists(d):
        raise CxdError("run %r already exists on %s (use resume, or another name)" % (name, self_label()), "exists")
    cwd = os.path.expanduser(args.get("cwd") or "~")
    if not os.path.isdir(cwd):
        raise CxdError("cwd %s does not exist on %s" % (cwd, self_label()), "usage")
    # With an account the run goes through `cswap run <account> --`; without
    # one (and no default_account in config.json) it is plain `claude`.
    account = args.get("account") or load_config().get("default_account") or None
    if account and not find_cswap():
        raise CxdError("cswap not found on %s (needed for account %r)" % (self_label(), account), "missing_cswap")
    if not account and not find_claude():
        raise CxdError("claude not found on %s" % self_label(), "missing_claude")
    # meta.json keeps only this run's own opt-in; the host's setting is read at each launch.
    opt_in = args.get("skip_permissions") is True
    ensure_dir(path("runs"))
    ensure_dir(d)
    meta = {
        "name": name, "host": self_label(), "account": account, "bypass_opt_in": opt_in,  # GUARD:host-not-kept
        "cwd": cwd, "claude_args": list(args.get("claude_args") or []), "gen": 1,
        "created": now_iso(), "session_id": None, "cxd_version": VERSION,
    }
    atomic_write_json(os.path.join(d, "meta.json"), meta)
    return _launch(d, meta, 1, None, prompt, float(args.get("init_timeout") or 90), opt_in)


def _launch(d, meta, gen, resume_sid, prompt, init_timeout, opt_in):
    """Start gen of a run. The run's opt-in is saved with its session id, once
    the gen has started; a launch that fails does not save an opt-in."""
    skip_permissions = bypass_opted_in(opt_in)
    _spawn_supervisor(d, gen, resume_sid, skip_permissions)
    pids = _wait_pids(d, gen)
    try:
        fifo_write(os.path.join(d, "in.fifo"), user_message(prompt))
        sid = _wait_session_id(d, gen, init_timeout)
    except CxdError as e:
        if e.code != "dead":
            raise
        sid = None
    if not sid and not claude_alive(pids):
        deadline = time.time() + 3
        ex = {}
        while time.time() < deadline:
            ex = read_json(os.path.join(d, "exit.json"), None) or {}
            if ex.get("gen") == gen:
                break
            time.sleep(0.1)
        raise CxdError("run %r exited during start (rc=%s); stderr: %s" % (
            meta["name"], ex.get("rc"), _tail(os.path.join(d, "err.log"))), "start_failed")
    meta = read_json(os.path.join(d, "meta.json"), meta)
    if sid:
        meta["session_id"] = sid
        ids = meta.get("session_ids") if isinstance(meta.get("session_ids"), list) else []
        if sid not in ids:
            meta["session_ids"] = ids + [sid]  # every gen's, so pruning keeps them with the run
    meta["bypass_opt_in"] = opt_in
    meta.pop("skip_permissions", None)  # cxd 0.1.0's key, never trusted
    atomic_write_json(os.path.join(d, "meta.json"), meta)
    return {"name": meta["name"], "host": self_label(), "gen": gen, "session_id": sid,
            "claude_pid": pids.get("claude_pid"), "keeper_pid": pids.get("keeper_pid"),
            "alive": claude_alive(pids), "run_dir": d, "skip_permissions": skip_permissions}


def _tail(p, n=400):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return " ".join(f.read().decode("utf-8", "replace").split()) or "(empty)"
    except OSError:
        return "(none)"


def op_resume(args):
    name = args.get("name")
    prompt = args.get("prompt")
    if not prompt:
        raise CxdError("empty prompt", "usage")
    d = _load_run(name)
    meta, pids, ex = _run_files(d)
    if claude_alive(pids):  # GUARD:resume-alive
        raise CxdError("run %r is still running (pid %s); stop it before resuming" % (name, pids.get("claude_pid")), "alive")
    # The supervisor normally reaps its keeper; clear an orphaned one (ours, verified).
    if keeper_alive(pids):
        signal_verified(pids.get("keeper_pid"), pids.get("keeper_ident"), signal.SIGTERM)
        wait_dead(pids.get("keeper_pid"), pids.get("keeper_ident"), 5)
    sid = meta.get("session_id") or _scan_session_id(os.path.join(d, "out.jsonl"))
    if not sid:
        raise CxdError("run %r never reported a session id; nothing to resume" % name, "no_session")
    gen = int(meta.get("gen") or 1) + 1
    if ex is not None:
        os.replace(os.path.join(d, "exit.json"), os.path.join(d, "exit.%d.json" % ex.get("gen", gen - 1)))
    meta["gen"] = gen
    meta["session_id"] = sid
    # A resumed run keeps its own opt-in; `cxd resume --skip-permissions` adds
    # one (saved once the gen started) and `--no-skip-permissions` withdraws it
    # (saved right away, whatever happens to the launch).
    opt_in = meta.get("bypass_opt_in") is True or args.get("skip_permissions") is True  # GUARD:resume-bypass
    if args.get("no_skip_permissions") is True:
        opt_in = False
        meta["bypass_opt_in"] = False  # GUARD:withdraw-now
        meta.pop("skip_permissions", None)
    atomic_write_json(os.path.join(d, "meta.json"), meta)
    return _launch(d, meta, gen, sid, prompt, float(args.get("init_timeout") or 90), opt_in)


def run_idle(d, quiet=2.0):
    """True when the last stream event is a turn result and the output has
    been quiet for `quiet` seconds: no turn is running and none is starting."""
    out = os.path.join(d, "out.jsonl")
    try:
        st = os.stat(out)
    except OSError:
        return False
    if time.time() - st.st_mtime < quiet:
        return False
    with open(out, "rb") as f:
        f.seek(max(0, st.st_size - 262144))
        tail = f.read().splitlines()
    for raw in reversed(tail):
        ev = _parse(raw.decode("utf-8", "replace"))
        if not ev or ev.get("type") == "cxd":
            continue
        if ev.get("type") == "system" and ev.get("subtype") not in ("init",):
            continue  # hook / task bookkeeping events
        return ev.get("type") == "result"
    return False


def op_stop(args):
    name = args.get("name")
    d = _load_run(name)
    grace = 0.0 if args.get("now") else float(args.get("grace", 120))
    int_wait = float(args.get("int_wait", 15))
    term_wait = float(args.get("term_wait", 10))
    meta, pids, ex = _run_files(d)
    cpid, cid = pids.get("claude_pid"), pids.get("claude_ident")
    kpid, kid = pids.get("keeper_pid"), pids.get("keeper_ident")
    steps = []
    t0 = time.time()
    if not claude_alive(pids):
        if signal_verified(kpid, kid, signal.SIGTERM):
            steps.append("keeper-orphan-terminated")
        return {"name": name, "stopped": True, "already_exited": True, "rc": (ex or {}).get("rc"),
                "steps": steps, "elapsed": 0.0}
    # 1. EOF: the keeper is the only writer, so ending it closes claude's stdin.
    #    On Linux claude exits on EOF within ~1 s. On macOS it does not notice
    #    EOF on a FIFO (observed with Claude Code 2.1.302), so we also watch for
    #    the current turn to end and then interrupt the idle session.
    if signal_verified(kpid, kid, signal.SIGTERM):  # GUARD:stop-signal
        steps.append("keeper-eof")
    dead = False
    t_eof = time.time()
    deadline = t_eof + grace
    while True:
        if not claude_alive(pids):
            dead = True
            steps.append("exited-on-eof")
            break
        # Give EOF a moment to work on its own (Linux) before interrupting.
        if time.time() - t_eof >= min(grace, 3.0) and run_idle(d):
            steps.append("turn-idle")
            break
        if time.time() >= deadline:
            if grace:
                steps.append("grace-expired")
            break
        time.sleep(0.2)
    # 2. SIGINT ends the current turn (none is running if idle); stdin is at
    #    EOF, so claude then exits.
    if not dead:
        if signal_verified(cpid, cid, signal.SIGINT):
            steps.append("sigint")
        dead = wait_dead(cpid, cid, int_wait)
    # 3. SIGTERM.
    if not dead:
        if signal_verified(cpid, cid, signal.SIGTERM):
            steps.append("sigterm")
        dead = wait_dead(cpid, cid, term_wait)
    rc = None
    if dead:
        deadline = time.time() + 5
        while time.time() < deadline:
            ex = read_json(os.path.join(d, "exit.json"), None)
            if ex and ex.get("gen") == pids.get("gen"):
                rc = ex.get("rc")
                break
            time.sleep(0.1)
    res = {"name": name, "stopped": dead, "rc": rc, "steps": steps,
           "elapsed": round(time.time() - t0, 2)}
    if not dead:
        raise CxdError("run %r is still alive after keeper EOF, SIGINT and SIGTERM (pid %s)" % (name, cpid),
                       "still_alive")
    return res


# ================================================================ sessions (hook path)

def _resolve_session(target):
    d = path("sessions")
    exact = os.path.join(d, target + ".json")
    if SID_RE.match(target or "") and os.path.isfile(exact):
        return read_json(exact, None)
    if len(target or "") >= 8 and os.path.isdir(d):
        hits = [n for n in os.listdir(d) if n.startswith(target) and n.endswith(".json")]
        if len(hits) == 1:
            return read_json(os.path.join(d, hits[0]), None)
        if len(hits) > 1:
            raise CxdError("session prefix %r is ambiguous (%d matches)" % (target, len(hits)), "ambiguous")
    return None


def session_alive(rec):
    if not rec or rec.get("ended"):
        return False
    return proc_alive(rec.get("pid"), rec.get("pid_ident"))


def _resolve(target):
    """('run', run_dir) or ('session', record)."""
    if NAME_RE.match(target or "") and os.path.isdir(path("runs", target)):
        return "run", path("runs", target)
    rec = _resolve_session(target)
    if rec:
        return "session", rec
    raise CxdError("no run or registered session %r on %s" % (target, self_label()), "not_found")


def new_msg_id():
    return "%019d-%06d" % (time.time_ns(), random.randint(0, 999999))


def op_send(args):
    target = args.get("target")
    text = args.get("text")
    if not text:
        raise CxdError("empty message", "usage")
    sender = args.get("sender") or "unknown"
    kind, ref = _resolve(target)
    msg_id = new_msg_id()
    if kind == "run":
        if not _run_accepts_input(ref):  # GUARD:send-alive
            _, _, ex = _run_files(ref)
            raise CxdError("run %r is not running%s; refusing to send" % (
                target, "" if not ex else " (exited rc=%s at %s)" % (ex.get("rc"), ex.get("ended"))), "dead")
        fifo_write(os.path.join(ref, "in.fifo"), user_message(text))
        return {"target": target, "via": "fifo", "id": msg_id}
    rec = ref
    sid = rec["session_id"]
    if not session_alive(rec):
        raise CxdError("session %s is not running (%s); refusing to send" % (
            sid, "ended " + rec["ended"] if rec.get("ended") else "process gone"), "dead")
    inbox = ensure_dir(path("inbox", sid))
    # A hook can hand over at most ~10,000 characters at a time, so a long
    # message is queued as numbered parts, each delivered whole, in order.
    if len(text) > MAX_MESSAGE:
        raise CxdError("message too long for a session (%d characters, at most %d); "
                       "write it to a file and send the path" % (len(text), MAX_MESSAGE), "usage")
    chunks = _split_text(text, PART_CHARS)
    ts = now_iso()
    ids = []
    for k, chunk in enumerate(chunks, 1):
        part_id = msg_id if len(chunks) == 1 else "%s-p%04d" % (msg_id, k)
        ids.append(part_id)
        body = chunk if len(chunks) == 1 else "[part %d/%d] %s" % (k, len(chunks), chunk)
        msg = {"id": part_id, "ts": ts, "sender": sender, "text": body}
        if isinstance(args.get("sender_sid"), str):
            msg["sender_sid"] = args["sender_sid"]  # a message to itself is not a lead instruction
        tmp = os.path.join(inbox, ".tmp-" + part_id)
        atomic_write_json(tmp, msg)
        os.replace(tmp, os.path.join(inbox, part_id + ".json"))
    return {"target": sid, "via": "inbox", "id": msg_id, "parts": len(chunks), "ids": ids}


PART_CHARS = 6000
MAX_MESSAGE = 1000000  # 167 parts at most


def _split_text(text, n):
    """Chunks of at most n characters that join back to text exactly; cut after
    a newline when there is one in the second half, else after a space."""
    chunks = []
    while len(text) > n:
        cut = text.rfind("\n", n // 2, n)
        if cut < 0:
            cut = text.rfind(" ", n // 2, n)
        cut = n if cut < 0 else cut + 1
        chunks.append(text[:cut])
        text = text[cut:]
    if text or not chunks:
        chunks.append(text)
    return chunks


def op_notify(args):
    sid = args.get("session_id")
    text = args.get("text")
    if not sid or not SID_RE.match(sid):
        raise CxdError("no session id (set CLAUDE_CODE_SESSION_ID or pass --session)", "usage")
    if not text:
        raise CxdError("empty notification", "usage")
    ensure_dir(path("outbox"))
    rec = {"ts": now_iso(), "kind": "notify", "session_id": sid, "text": text[:4000]}
    append_jsonl(path("outbox", sid + ".jsonl"), rec)
    return {"session_id": sid, "ok": True}


# ================================================================ events (watch)

def _scan_out(out, cur, emit):
    """Scan new complete lines of out.jsonl from cur['out'] and emit events.

    A line can give several events (PR links, then the turn). All but the last
    carry a cursor at the start of the line, the last one a cursor after it: a
    client that resumes inside a line reads it again and skips the PRs it got.
    emit returns the cursor it gave the event (or None)."""
    try:
        size = os.path.getsize(out)
    except OSError:
        return
    off = cur.get("out", 0)
    if size <= off:
        return
    with open(out, "rb") as f:
        f.seek(off)
        chunk = f.read(min(size - off, 8 * 1024 * 1024))
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    seen_pr = set(cur.get("prs", []))
    pos = off
    for raw in chunk[: end + 1].split(b"\n")[:-1]:
        start, pos = pos, pos + len(raw) + 1
        cur["out"] = start  # GUARD:event-line-start
        last = _scan_line(raw, cur, seen_pr, emit)
        cur["out"] = pos
        if isinstance(last, dict):
            last["out"] = pos  # the line's last event resumes after it
    cur["prs"] = sorted(seen_pr)


def _scan_line(raw, cur, seen_pr, emit):
    """The events of one line of out.jsonl; returns the last one's cursor."""
    last = None
    ev = _parse(raw.decode("utf-8", "replace"))
    if ev:
        t = ev.get("type")
        if t == "cxd" and ev.get("event") == "gen-start":
            cur["gen"] = ev.get("gen")
        elif t == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                cur.setdefault("sids", [])
                if sid not in cur["sids"]:
                    cur["sids"].append(sid)
            last = emit({"ev": "init", "session_id": sid, "model": ev.get("model"), "gen": cur.get("gen")})
        elif t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    for url in PR_RE.findall(block.get("text") or ""):
                        if url not in seen_pr:
                            seen_pr.add(url)
                            cur["prs"] = sorted(seen_pr)
                            last = emit({"ev": "pr", "url": url})
        elif t == "result":
            res = ev.get("result")
            for url in PR_RE.findall(res or ""):
                if url not in seen_pr:
                    seen_pr.add(url)
                    cur["prs"] = sorted(seen_pr)
                    last = emit({"ev": "pr", "url": url})
            # Counted after the line's PRs: a resume after one of them reads it again.
            cur["turns"] = cur.get("turns", 0) + 1
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                # total_cost_usd is cumulative within one process; keep the last per gen.
                cur.setdefault("gen_costs", {})[str(cur.get("gen"))] = cost
            last = emit({"ev": "turn", "turn": cur["turns"], "is_error": bool(ev.get("is_error")),
                         "subtype": ev.get("subtype"), "num_turns": ev.get("num_turns"),
                         "cost_usd": cost, "duration_ms": ev.get("duration_ms"),
                         "result": excerpt(res, 400), "session_id": ev.get("session_id")})
    return last


def _scan_outbox(sid, cur, emit, skip_kinds=()):
    p = path("outbox", sid + ".jsonl")
    offs = cur.setdefault("outbox", {})
    try:
        size = os.path.getsize(p)
    except OSError:
        return
    off = offs.get(sid, 0)
    if size <= off:
        return
    with open(p, "rb") as f:
        f.seek(off)
        chunk = f.read(size - off)
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    pos = off
    for raw in chunk[: end + 1].split(b"\n")[:-1]:
        pos += len(raw) + 1
        offs[sid] = pos  # per line, as in _scan_out
        rec = _parse(raw.decode("utf-8", "replace"))
        if not rec or rec.get("kind") in skip_kinds:
            continue
        emit({"ev": "outbox", "kind": rec.get("kind"), "session_id": sid, "ts": rec.get("ts"),
              "text": excerpt(rec.get("text"), 600), "id": rec.get("id"), "event": rec.get("event")})


# How long watch waits, after claude is gone, for its live supervisor to record
# the exit code. The supervisor writes exit.json right after reaping claude.
EXIT_RECORD_WAIT = 10.0


def _exit_record(d, gen):
    """The supervisor's exit record of gen: exit.json, or exit.<gen>.json once a
    resume has moved it aside. {} while there is none."""
    if gen is None:
        return {}
    for name in ("exit.json", "exit.%s.json" % gen):
        ex = read_json(os.path.join(d, name), None) or {}
        if ex.get("gen") == gen:
            return ex
    return {}


def _scan_run(d, meta, cur, emit):
    """A run's events: its output, then its sessions' reports."""
    _scan_out(os.path.join(d, "out.jsonl"), cur, emit)
    for sid in list(cur.get("sids", [])) or ([meta["session_id"]] if meta.get("session_id") else []):
        _scan_outbox(sid, cur, emit, skip_kinds=("stop",))  # turn events already carry results


def run_cost(cur):
    return round(sum((cur.get("gen_costs") or {}).values()), 6)


def poll(target, cur):
    """One scan. Returns (events, cursor). Emits 'exit' once, from host state only.
    Each event carries, under "_cursor", the cursor as it stands right after
    that event: a client that loses the stream between two events of one scan
    resumes exactly after the last one it received."""
    events = []
    kind, ref = _resolve(target)
    cur = json.loads(json.dumps(cur or {}))  # never change the caller's cursor

    def emit(e):
        e["_cursor"] = json.loads(json.dumps(cur))  # GUARD:event-cursor
        events.append(e)
        return e["_cursor"]
    cur["kind"] = kind
    if kind == "run":
        d = ref
        meta, pids, _ = _run_files(d)
        _scan_run(d, meta, cur, emit)
        gen = pids.get("gen")
        if not pids:
            # No pids at all: the supervisor of the first gen failed before it
            # started claude. Its exit record says why; nothing else will happen.
            ex = _exit_record(d, meta.get("gen"))
            if ex and cur.get("exit_gen") != ex["gen"]:
                cur["exit_gen"] = ex["gen"]
                emit({"ev": "exit", "rc": ex.get("rc"), "gen": ex["gen"], "error": ex.get("error"),
                      "turns": cur.get("turns", 0), "cost_usd": run_cost(cur)})
        elif claude_alive(pids):
            cur.pop("exit_wait", None)
        elif cur.get("exit_gen") != gen:
            ex = _exit_record(d, gen)
            # The supervisor reaps claude, then writes exit.json: while it lives,
            # wait (bounded) for the rc rather than report the exit without it.
            if not ex and supervisor_alive(pids):  # GUARD:exit-rc
                wait = cur.get("exit_wait")
                if not (isinstance(wait, dict) and wait.get("gen") == gen):
                    wait = cur["exit_wait"] = {"gen": gen, "t": time.time()}
                waited = time.time() - wait["t"]
                if waited < 0:  # the clock stepped back: start the wait again
                    wait["t"], waited = time.time(), 0.0
                if waited < EXIT_RECORD_WAIT:  # GUARD:exit-rc-cap
                    for e in events:  # resuming after one of them continues this wait
                        e["_cursor"]["exit_wait"] = dict(wait)
                    return events, cur
            ex = ex or _exit_record(d, gen)  # written just before the supervisor exited?
            cur.pop("exit_wait", None)
            # Final flush after the process ended, reports included: watch ends at the exit.
            _scan_run(d, meta, cur, emit)  # GUARD:exit-flush-outbox
            cur["exit_gen"] = gen
            emit({"ev": "exit", "rc": ex.get("rc"), "gen": gen, "turns": cur.get("turns", 0),
                  "cost_usd": run_cost(cur)})
    else:
        rec = ref
        sid = rec["session_id"]
        _scan_outbox(sid, cur, emit)
        if not session_alive(rec) and not cur.get("exit_reported"):
            _scan_outbox(sid, cur, emit)
            cur["exit_reported"] = True
            emit({"ev": "exit", "rc": None,
                  "reason": "session ended %s" % rec["ended"] if rec.get("ended") else "session process gone"})
    return events, cur


def status_of(target, cur):
    kind, ref = _resolve(target)
    if kind == "run":
        meta, pids, ex = _run_files(ref)
        return {"kind": "run", "alive": claude_alive(pids), "keeper_alive": keeper_alive(pids),
                "gen": meta.get("gen"), "session_id": meta.get("session_id"),
                "turns": cur.get("turns", 0), "cost_usd": run_cost(cur)}
    return {"kind": "session", "alive": session_alive(ref), "session_id": ref.get("session_id"),
            "cwd": ref.get("cwd"), "account": ref.get("account")}


def stream(args):
    """Generator of messages for `cxd watch`. Ends after the exit event."""
    target = args.get("target")
    heartbeat = float(args.get("heartbeat", 60))
    keepalive = float(args.get("keepalive", 10))
    interval = float(args.get("interval", 0.5))
    cur = args.get("cursor")
    yield {"type": "hello", "host": self_label(), "cursor": cur}  # the channel works
    if cur is None:
        _resolve(target)  # raise not_found before streaming
        if args.get("from_start"):
            cur = {}
        else:
            _, cur = poll(target, {"exit_gen": None})
            cur.pop("exit_gen", None)
            cur.pop("exit_reported", None)
        st = status_of(target, cur)
        yield {"type": "event", "event": dict(st, ev="status"), "cursor": cur}
    last_hb = last_msg = time.time()
    while True:
        events, cur = poll(target, cur)
        for e in events:
            last_msg = time.time()
            at = e.pop("_cursor", cur)
            yield {"type": "event", "event": e, "cursor": at}
            if e["ev"] == "exit":
                yield {"type": "end", "cursor": at}
                return
        now = time.time()
        if now - last_hb >= heartbeat:
            last_hb = last_msg = now
            yield {"type": "event", "event": dict(status_of(target, cur), ev="heartbeat"), "cursor": cur}
        elif now - last_msg >= keepalive:
            last_msg = now
            yield {"type": "keepalive", "cursor": cur}
        time.sleep(interval)


# ================================================================ list

def op_list(args):
    show_all = bool(args.get("all"))
    runs = []
    rd = path("runs")
    if os.path.isdir(rd):
        for name in sorted(os.listdir(rd)):
            d = os.path.join(rd, name)
            if not os.path.isdir(d):
                continue
            meta, pids, ex = _run_files(d)
            cur = {}
            _scan_out(os.path.join(d, "out.jsonl"), cur, lambda e, _c=cur: _c.__setitem__("last", e) if e["ev"] == "turn" else None)
            last = cur.get("last") or {}
            runs.append({
                "name": name, "alive": claude_alive(pids), "keeper_alive": keeper_alive(pids),
                "gen": meta.get("gen"), "account": meta.get("account"), "session_id": meta.get("session_id"),
                "turns": cur.get("turns", 0), "cost_usd": run_cost(cur),
                "last_result": excerpt(last.get("result"), 120), "rc": (ex or {}).get("rc"),
                "cwd": meta.get("cwd"), "created": meta.get("created"),
            })
    sessions = []
    sd = path("sessions")
    if os.path.isdir(sd):
        for n in sorted(os.listdir(sd)):
            if not n.endswith(".json"):
                continue
            rec = read_json(os.path.join(sd, n), None)
            if not rec:
                continue
            alive = session_alive(rec)
            if not alive and not show_all:
                continue
            pending = 0
            for box in ("inbox", "claimed"):  # claimed: handed to a hook, not yet acknowledged
                d = path(box, rec.get("session_id", ""))
                pending += len([m for m in os.listdir(d) if m.endswith(".json")]) if os.path.isdir(d) else 0
            sessions.append({
                "session_id": rec.get("session_id"), "alive": alive, "account": rec.get("account"),
                "cwd": rec.get("cwd"), "registered": rec.get("registered"), "pid": rec.get("pid"),
                "cxd_run": rec.get("cxd_run"), "inbox_pending": pending, "ended": rec.get("ended"),
            })
    return {"host": self_label(), "runs": runs, "sessions": sessions}


# ================================================================ ping / doctor

def op_ping(args):
    return {"pong": True, "host": self_label(), "version": VERSION, "proto": PROTO,
            "python": sys.version.split()[0], "time": time.time()}


def _run_capture(cmd, timeout, env=None, stdin_text=None):
    try:
        r = subprocess.run(cmd, input=stdin_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout, env=env or worker_env())
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timeout after %ss" % timeout
    except OSError as e:
        return None, "", str(e)


def _json_tail(text):
    """Parse the JSON document in text, skipping cswap's 'Launching' preamble."""
    i = min([p for p in (text.find("["), text.find("{")) if p >= 0] or [-1])
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except ValueError:
        return None


def hook_selftest():
    """Run the real hook script against a throwaway CXD_HOME: an empty inbox
    must give no output, a queued message must come back as additionalContext
    exactly once."""
    import tempfile
    import shutil
    hook = os.path.join(os.path.dirname(cxd_bin()), "..", "hooks", "cxd-hook")
    hook = os.path.normpath(hook)
    tmp = tempfile.mkdtemp(prefix="cxd-selftest-")
    try:
        env = dict(os.environ, CXD_HOME=os.path.join(tmp, "h"))
        sid = "selftest-%d" % os.getpid()
        payload = json.dumps({"session_id": sid, "hook_event_name": "PostToolUse", "cwd": tmp})
        t0 = time.time()
        rc0, out0, _ = _run_capture([hook, "PostToolUse"], 10, env=env, stdin_text=payload)
        empty_ms = round((time.time() - t0) * 1000, 1)
        inbox = os.path.join(tmp, "h", "inbox", sid)
        os.makedirs(inbox, mode=0o700)
        with open(os.path.join(inbox, "0001-000001.json"), "w") as f:
            json.dump({"id": "0001-000001", "ts": now_iso(), "sender": "doctor", "text": "SELFTEST-MARK"}, f)
        t0 = time.time()
        rc1, out1, _ = _run_capture([hook, "PostToolUse"], 10, env=env, stdin_text=payload)
        full_ms = round((time.time() - t0) * 1000, 1)
        rc2, out2, _ = _run_capture([hook, "PostToolUse"], 10, env=env, stdin_text=payload)
        ctx = ""
        try:
            ctx = json.loads(out1)["hookSpecificOutput"]["additionalContext"]
        except Exception:
            pass
        ok = (rc0 == 0 and out0.strip() == "" and rc1 == 0 and "SELFTEST-MARK" in ctx
              and rc2 == 0 and out2.strip() == "")
        return {"ok": ok, "empty_inbox_ms": empty_ms, "delivery_ms": full_ms,
                "delivered_once": "SELFTEST-MARK" in ctx and out2.strip() == ""}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def op_doctor_info(args):
    cfg = load_config()
    accounts = args.get("accounts") or cfg.get("accounts") or []
    info = {"host": self_label(), "version": VERSION, "python": sys.version.split()[0],
            "platform": sys.platform, "checks": []}

    def check(name, ok, detail):
        info["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    home = cxd_home()
    ensure_dir(home)
    mode = oct(os.stat(home).st_mode & 0o777)
    check("cxd home mode 700", mode == "0o700", "%s %s" % (home, mode))
    cswap = find_cswap()
    if accounts or cfg.get("default_account"):
        check("cswap present", bool(cswap), cswap or "not found")
    else:
        check("cswap present (optional: only needed for --account)", True, cswap or "not installed")
    claude = find_claude()
    rc, out, err = _run_capture([claude, "--version"], 20) if claude else (None, "", "not found")
    check("claude CLI", rc == 0, (out.strip() or err.strip())[:80])
    st = hook_selftest()
    check("hook self-test (empty inbox silent, message delivered once)", st["ok"],
          "empty %.0f ms, delivery %.0f ms" % (st["empty_inbox_ms"], st["delivery_ms"]))
    regs = read_json(path("registrations.json"), {})  # kept by the SessionStart hook
    regs = dict(regs) if isinstance(regs, dict) else {}
    sd = path("sessions")
    if os.path.isdir(sd):
        for n in os.listdir(sd):
            rec = read_json(os.path.join(sd, n), None) or {}
            acct = rec.get("account") or "?"
            if rec.get("registered", "") > regs.get(acct, ""):
                regs[acct] = rec.get("registered", "")
    # Each configured cswap account has its own plugin install; with no
    # accounts configured, check the default Claude Code profile.
    targets = [(acct, [cswap, "run", acct, "--"] if cswap else None) for acct in accounts]
    if not accounts:
        targets = [("the default profile", [claude] if claude else None)]
    for acct, prefix in targets:
        if not prefix:
            check("plugin enabled for %s" % acct, False, "no cswap" if accounts else "no claude")
            continue
        rc, out, err = _run_capture(prefix + ["plugin", "list", "--json"], 90)
        plugins = _json_tail(out) or []
        mine = [p for p in plugins if isinstance(p, dict) and str(p.get("id", "")).startswith("cswap-cross-device@")]
        ok = bool(mine) and bool(mine[0].get("enabled"))
        detail = ("%s enabled=%s %s" % (mine[0].get("id"), mine[0].get("enabled"), mine[0].get("installPath"))
                  if mine else "not installed (rc=%s %s)" % (rc, (err or "").strip()[:120]))
        check("plugin enabled for %s" % acct, ok, detail)
    info["hook_registrations"] = regs
    info["ok"] = all(c["ok"] for c in info["checks"])
    return info


def op_prune(args):
    from .prune import prune
    older = args.get("older_than_days")
    if older is not None and (isinstance(older, bool) or not isinstance(older, (int, float))):
        raise CxdError("--older-than must be a number of days", "usage")
    import fcntl
    fd = os.open(path("prune.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # one pass at a time (with the automatic one)
        return dict(prune(load_config(), older_than_days=older, runs=bool(args.get("runs")),
                          dry_run=bool(args.get("dry_run"))), host=self_label())
    finally:
        os.close(fd)


OPS = {
    "ping": op_ping,
    "prune": op_prune,
    "start": op_start,
    "send": op_send,
    "stop": op_stop,
    "resume": op_resume,
    "list": op_list,
    "notify": op_notify,
    "doctor_info": op_doctor_info,
}
