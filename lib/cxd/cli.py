"""cxd command line."""

import argparse
import json
import os
import sys
import time

from .common import (
    CxdError, EXIT_ERROR, EXIT_NOT_APPLIED, EXIT_OK, EXIT_REFUSED, EXIT_TRANSPORT, EXIT_USAGE, PROTO,
    TransportError, VERSION, atomic_write_json, cxd_bin, ensure_dir, excerpt, load_config,
    path, self_label,
)
from .transport import get_transport, known_hosts

USAGE = """cxd - lead <-> worker messaging for Claude Code sessions across cswap accounts and machines

  cxd start  <host> <name> <prompt-file|-> [--account A] [--cwd DIR] [--skip-permissions]
             [-- claude args...]
  cxd send   <host> <run-name|session-id> <text|@file|->
  cxd watch  <host> <run-name|session-id> [--json] [--heartbeat S] [--from-start]
  cxd list   [host] [--all] [--json]
  cxd stop   <host> <name> [--now] [--grace S]
  cxd resume <host> <name> <prompt-file|-> [--skip-permissions | --no-skip-permissions]
  cxd notify <text|@file|-> [--session SID]      (inside a worker session)
  cxd doctor [host...] [--json]
  cxd init   [--self NAME] [--peer HOST ...] [--account A ...] [--default-account A]

host: this machine's name (see `cxd init`), 'local', or a configured/ssh-reachable host.
"""


def _out(s=""):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def _err(s):
    sys.stderr.write("cxd: " + s + "\n")
    sys.stderr.flush()


def _read_text(arg):
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        with open(os.path.expanduser(arg[1:]), "r", encoding="utf-8") as f:
            return f.read()
    return arg


def _read_prompt_file(arg):
    if arg == "-":
        return sys.stdin.read()
    p = os.path.expanduser(arg[1:] if arg.startswith("@") else arg)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _sender():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    acct = os.path.basename((os.environ.get("CLAUDE_CONFIG_DIR") or "").rstrip("/")) or None
    who = self_label()
    if sid:
        who += "/" + sid[:8]
    if acct:
        who += " (" + acct + ")"
    return who


def _ts():
    return time.strftime("%H:%M:%S")


PERMISSIONS_WARNING = (
    "warning: %s/%s runs without --dangerously-skip-permissions. Nobody can answer a permission prompt "
    "in a headless run, so a tool call that needs one is refused and the work may stall. Allow what the "
    "task needs after --, e.g. `-- --permission-mode acceptEdits` or `-- --allowedTools \"Bash(git *)\"`; "
    "or opt in to skipping every check with `cxd start ... --skip-permissions` or "
    "\"skip_permissions\": true in that host's ~/.cxd/config.json."
)

RESUME_PERMISSIONS_WARNING = (
    "warning: %s/%s resumes without --dangerously-skip-permissions. Nobody can answer a permission prompt "
    "in a headless run, so a tool call that needs one is refused and the work may stall. A resumed run "
    "keeps the claude arguments it was started with. To skip every check, opt in with "
    "`cxd resume %s %s <prompt> --skip-permissions` or \"skip_permissions\": true in that host's "
    "~/.cxd/config.json."
)

BYPASS_NOT_APPLIED = (
    "%s did not apply --skip-permissions (does it run an older cxd? update it and run `cxd init` there): "
    "%s/%s is running WITHOUT the bypass"
)


# ---------------------------------------------------------------- commands

def cmd_start(a):
    t = get_transport(a.host)
    prompt = _read_prompt_file(a.prompt_file)
    cwd = a.cwd
    if cwd is None and t.kind == "local":
        cwd = os.getcwd()
    res = t.call("start", {"name": a.name, "prompt": prompt, "account": a.account, "cwd": cwd,
                           "skip_permissions": a.skip_permissions,
                           "claude_args": a.claude_args, "init_timeout": a.init_timeout})
    if a.json:
        _out(json.dumps(res))
    else:
        _out("started %s/%s gen=%s session=%s claude_pid=%s alive=%s" % (
            a.host, res["name"], res["gen"], res.get("session_id"), res.get("claude_pid"), res.get("alive")))
    return _bypass_outcome(a, res, PERMISSIONS_WARNING % (a.host, res["name"]))


def cmd_resume(a):
    t = get_transport(a.host)
    prompt = _read_prompt_file(a.prompt_file)
    res = t.call("resume", {"name": a.name, "prompt": prompt, "init_timeout": a.init_timeout,
                            "skip_permissions": a.skip_permissions,
                            "no_skip_permissions": a.no_skip_permissions})
    if a.json:
        _out(json.dumps(res))
    else:
        _out("resumed %s/%s gen=%s session=%s claude_pid=%s" % (
            a.host, res["name"], res["gen"], res.get("session_id"), res.get("claude_pid")))
    return _bypass_outcome(a, res, RESUME_PERMISSIONS_WARNING % (a.host, res["name"], a.host, res["name"]))


def _bypass_outcome(a, res, warning):
    """After a start or resume: exit 5 if the host did not apply a requested
    --skip-permissions (the run IS running, without it), else warn when the run
    has no bypass."""
    if a.skip_permissions and not res.get("skip_permissions"):  # GUARD:bypass-not-applied
        _err(BYPASS_NOT_APPLIED % (a.host, a.host, res["name"]))
        return EXIT_NOT_APPLIED
    if not res.get("skip_permissions"):
        _err(warning)
    return EXIT_OK


def cmd_send(a):
    t = get_transport(a.host)
    text = _read_text(a.text)
    res = t.call("send", {"target": a.target, "text": text, "sender": _sender()})
    _out(json.dumps(res) if a.json else "sent to %s/%s via %s id=%s" % (a.host, res["target"], res["via"], res["id"]))
    return EXIT_OK


def cmd_stop(a):
    t = get_transport(a.host)
    args = {"name": a.name, "now": a.now, "int_wait": a.int_wait, "term_wait": a.term_wait}
    if a.grace is not None:
        args["grace"] = a.grace
    res = t.call("stop", args, timeout=(a.grace or 120) + 120)
    if a.json:
        _out(json.dumps(res))
    elif res.get("already_exited"):
        _out("%s/%s had already exited rc=%s" % (a.host, a.name, res.get("rc")))
    else:
        _out("stopped %s/%s rc=%s in %.1fs via %s" % (a.host, a.name, res.get("rc"), res.get("elapsed", 0),
                                                       " -> ".join(res.get("steps") or ["nothing"])))
    return EXIT_OK


def cmd_notify(a):
    from .state import op_notify
    sid = a.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    res = op_notify({"session_id": sid, "text": _read_text(a.text)})
    _out("noted in outbox of session %s" % res["session_id"])
    return EXIT_OK


def _usd(v):
    return "$%.4f" % v if isinstance(v, (int, float)) else "n/a"


def _fmt_event(host, target, e):
    tag = "[cxd %s %s/%s]" % (_ts(), host, target)
    ev = e.get("ev")
    if ev == "status":
        if e.get("kind") == "run":
            return "%s status alive=%s gen=%s turns=%s cost=$%.4f session=%s" % (
                tag, e.get("alive"), e.get("gen"), e.get("turns"), e.get("cost_usd") or 0, e.get("session_id"))
        return "%s status session alive=%s cwd=%s account=%s" % (tag, e.get("alive"), e.get("cwd"), e.get("account"))
    if ev == "heartbeat":
        return "%s heartbeat alive=%s turns=%s" % (tag, e.get("alive"), e.get("turns", "-"))
    if ev == "init":
        return "%s session %s started (gen %s, model %s)" % (tag, e.get("session_id"), e.get("gen"), e.get("model"))
    if ev == "turn":
        return "%s turn %s ended%s cost=%s: %s" % (
            tag, e.get("turn"), " (ERROR %s)" % e.get("subtype") if e.get("is_error") else "",
            _usd(e.get("cost_usd")), e.get("result"))
    if ev == "pr":
        return "%s PR %s" % (tag, e.get("url"))
    if ev == "outbox":
        kind = e.get("kind")
        if kind == "delivered":
            return "%s delivered message %s via %s hook" % (tag, e.get("id"), e.get("event"))
        return "%s report (%s): %s" % (tag, kind, e.get("text"))
    if ev == "exit":
        if "reason" in e:
            return "%s EXITED: %s" % (tag, e.get("reason"))
        return "%s EXITED rc=%s gen=%s turns=%s cost=%s%s" % (
            tag, e.get("rc"), e.get("gen"), e.get("turns"), _usd(e.get("cost_usd")),
            " error: %s" % e["error"] if e.get("error") else "")
    return "%s %s" % (tag, json.dumps(e))


def cmd_watch(a):
    t = get_transport(a.host)
    cursor = None
    backoff = 2.0
    args = {"target": a.target, "heartbeat": a.heartbeat, "from_start": a.from_start}
    while True:
        try:
            got_any = False
            for msg in t.stream("stream", dict(args, cursor=cursor)):
                cursor = msg.get("cursor", cursor)
                got_any = True
                if msg.get("type") == "event":
                    e = msg["event"]
                    _out(json.dumps(dict(e, host=a.host, target=a.target, ts=time.time())) if a.json
                         else _fmt_event(a.host, a.target, e))
                    if e.get("ev") == "exit":
                        return EXIT_OK
                elif msg.get("type") == "end":
                    return EXIT_OK
            raise TransportError("stream ended without an exit event")
        except TransportError as e:  # GUARD:watch-transport
            # The channel failed. The run's state is unknown, NOT exited: only
            # the host itself may report an exit. Reconnect from the cursor.
            if got_any:
                backoff = 2.0
            line = "[cxd %s %s/%s] transport lost (%s); run state unknown, not exited; retrying in %.0fs" % (
                _ts(), a.host, a.target, e, backoff)
            _out(json.dumps({"ev": "transport", "host": a.host, "target": a.target, "error": str(e),
                             "retry_s": backoff}) if a.json else line)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def cmd_list(a):
    hosts = [a.host] if a.host else known_hosts()
    rc = EXIT_OK
    results = []
    for h in hosts:
        try:
            res = get_transport(h).call("list", {"all": a.all})
            res["host"] = h
            results.append(res)
        except TransportError as e:
            results.append({"host": h, "error": "transport: %s" % e})
            rc = EXIT_TRANSPORT
        except CxdError as e:
            results.append({"host": h, "error": str(e)})
            rc = EXIT_ERROR
    if a.json:
        _out(json.dumps(results))
        return rc
    for res in results:
        h = res["host"]
        if "error" in res:
            _out("%s: UNREACHABLE (%s)" % (h, res["error"]))
            continue
        _out("%s: %d run(s), %d %ssession(s)" % (h, len(res["runs"]), len(res["sessions"]), "" if a.all else "live "))
        for r in res["runs"]:
            _out("  run %-20s %-6s gen=%s turns=%s cost=$%.4f acct=%s session=%s last: %s" % (
                r["name"], "ALIVE" if r["alive"] else "exited(rc=%s)" % r.get("rc"), r.get("gen"), r["turns"],
                r["cost_usd"] or 0, r.get("account") or "-", (r.get("session_id") or "-")[:8], r.get("last_result") or "-"))
        for s in res["sessions"]:
            _out("  session %s %-5s acct=%s pid=%s inbox=%d%s cwd=%s" % (
                s["session_id"], "ALIVE" if s["alive"] else "dead", s.get("account"), s.get("pid"),
                s.get("inbox_pending", 0), " run=" + s["cxd_run"] if s.get("cxd_run") else "", s.get("cwd")))
    return rc


def cmd_doctor(a):
    hosts = a.hosts or known_hosts()
    all_ok = True
    report = []
    for h in hosts:
        entry = {"host": h}
        try:
            t = get_transport(h)
            t0 = time.time()
            pong = t.call("ping", {}, timeout=60)
            entry["ping_ms"] = round((time.time() - t0) * 1000, 1)
            entry["transport"] = t.describe()
            entry["remote_version"] = pong.get("version")
            info = t.call("doctor_info", {}, timeout=600)
            entry.update(info)
        except (TransportError, CxdError) as e:
            entry["ok"] = False
            entry["error"] = str(e)
        all_ok = all_ok and bool(entry.get("ok"))
        report.append(entry)
    if a.json:
        _out(json.dumps(report, indent=1))
    else:
        for e in report:
            if "error" in e:
                _out("%s: FAIL %s" % (e["host"], e["error"]))
                continue
            _out("%s: %s  (%s round-trip %.0f ms, cxd %s, python %s)" % (
                e["host"], "OK" if e.get("ok") else "FAIL", e.get("transport"), e.get("ping_ms", 0),
                e.get("version"), e.get("python")))
            for c in e.get("checks", []):
                _out("  [%s] %s: %s" % ("ok" if c["ok"] else "FAIL", c["name"], c["detail"]))
            regs = e.get("hook_registrations") or {}
            for acct, ts in sorted(regs.items()):
                _out("  [info] last SessionStart hook registration for config %s: %s" % (acct, ts))
    return EXIT_OK if all_ok else EXIT_ERROR


def _link_cli():
    """Point ~/.cxd/bin/cxd at this copy of cxd. Peers reach a host through
    that path over ssh, wherever the plugin or clone lives on it. Re-run
    `cxd init` after moving or updating the plugin."""
    target = os.path.realpath(cxd_bin())
    link = os.path.join(ensure_dir(path("bin")), "cxd")
    tmp = link + ".tmp.%d" % os.getpid()
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return link, target


def cmd_init(a):
    ensure_dir(path())
    cfg = load_config()
    if a.self_name:
        cfg["self"] = [a.self_name]
    for peer in a.peer or []:
        cfg["hosts"].setdefault(peer, {"transport": "ssh"})
    for acct in a.account or []:
        if acct not in cfg["accounts"]:
            cfg["accounts"].append(acct)
    if a.default_account is not None:
        cfg["default_account"] = a.default_account or None
    atomic_write_json(path("config.json"), cfg)
    link, target = _link_cli()
    _out(json.dumps(cfg, indent=1))
    _out("linked %s -> %s" % (link, target))
    return EXIT_OK


# ---------------------------------------------------------------- internal entry points

def internal_rpc():
    """`cxd _rpc`: one JSON request on stdin, JSON reply/stream on stdout. Exit 0 always
    (ssh itself exits 255, so a non-zero rc can only mean the channel failed)."""
    from . import state
    op = None
    try:
        req = json.loads(sys.stdin.read() or "{}")
        op = req.get("op")
        args = req.get("args") or {}
        if req.get("v") != PROTO:
            raise CxdError("protocol mismatch: client v%s, host v%s" % (req.get("v"), PROTO), "proto")
        if op == "stream":
            try:
                for msg in state.stream(args):
                    sys.stdout.write(json.dumps(msg) + "\n")
                    sys.stdout.flush()
            except (BrokenPipeError, IOError):
                pass
            return 0
        fn = state.OPS.get(op)
        if fn is None:
            raise CxdError("unknown op %r" % op, "usage")
        reply = {"ok": True, "result": fn(args)}
    except CxdError as e:
        reply = {"ok": False, "error": str(e), "code": e.code}
    except Exception as e:
        reply = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "code": "internal"}
    if op == "stream":
        reply = {"type": "error", "error": reply["error"], "code": reply["code"]}
    try:
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="cxd", usage=USAGE, add_help=True)
    p.add_argument("--version", action="version", version="cxd " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("start")
    s.add_argument("host"); s.add_argument("name"); s.add_argument("prompt_file")
    s.add_argument("--account"); s.add_argument("--cwd"); s.add_argument("--skip-permissions", action="store_true")
    s.add_argument("--init-timeout", type=float, default=90.0); s.add_argument("--json", action="store_true")

    s = sub.add_parser("resume")
    s.add_argument("host"); s.add_argument("name"); s.add_argument("prompt_file")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--skip-permissions", action="store_true"); g.add_argument("--no-skip-permissions", action="store_true")
    s.add_argument("--init-timeout", type=float, default=90.0); s.add_argument("--json", action="store_true")

    s = sub.add_parser("send")
    s.add_argument("host"); s.add_argument("target"); s.add_argument("text"); s.add_argument("--json", action="store_true")

    s = sub.add_parser("watch")
    s.add_argument("host"); s.add_argument("target"); s.add_argument("--json", action="store_true")
    s.add_argument("--heartbeat", type=float, default=60.0); s.add_argument("--from-start", action="store_true")

    s = sub.add_parser("list")
    s.add_argument("host", nargs="?"); s.add_argument("--all", action="store_true"); s.add_argument("--json", action="store_true")

    s = sub.add_parser("stop")
    s.add_argument("host"); s.add_argument("name"); s.add_argument("--now", action="store_true")
    s.add_argument("--grace", type=float); s.add_argument("--json", action="store_true")
    s.add_argument("--int-wait", type=float, default=15.0); s.add_argument("--term-wait", type=float, default=10.0)

    s = sub.add_parser("notify")
    s.add_argument("text"); s.add_argument("--session")

    s = sub.add_parser("doctor")
    s.add_argument("hosts", nargs="*"); s.add_argument("--json", action="store_true")

    s = sub.add_parser("init")
    s.add_argument("--self", dest="self_name"); s.add_argument("--peer", action="append")
    s.add_argument("--account", action="append"); s.add_argument("--default-account")
    return p


COMMANDS = {
    "start": cmd_start, "resume": cmd_resume, "send": cmd_send, "stop": cmd_stop,
    "watch": cmd_watch, "list": cmd_list, "notify": cmd_notify, "doctor": cmd_doctor, "init": cmd_init,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_hook":
        from .hook import main as hook_main
        return hook_main(argv[1] if len(argv) > 1 else "")
    if argv and argv[0] == "_rpc":
        return internal_rpc()
    if argv and argv[0] == "_supervise":
        from .supervise import supervise_main
        ap = argparse.ArgumentParser(prog="cxd _supervise")
        ap.add_argument("rundir"); ap.add_argument("--gen", type=int, required=True); ap.add_argument("--resume")
        ap.add_argument("--skip-permissions", action="store_true")
        ns = ap.parse_args(argv[1:])
        return supervise_main(ns.rundir, ns.gen, ns.resume, ns.skip_permissions)
    if argv and argv[0] == "_keeper":
        from .supervise import keeper_main
        return keeper_main(argv[1], argv[2])

    claude_args = []
    if argv and argv[0] == "start" and "--" in argv:
        i = argv.index("--")
        argv, claude_args = argv[:i], argv[i + 1:]
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        sys.stdout.write(USAGE)
        return EXIT_USAGE
    a.claude_args = claude_args
    try:
        return COMMANDS[a.cmd](a)
    except CxdError as e:
        _err("%s [%s]" % (e, e.code))
        return EXIT_USAGE if e.code == "usage" else EXIT_REFUSED
    except TransportError as e:
        _err("transport failure: %s" % e)
        return EXIT_TRANSPORT
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        _err(str(e))
        return EXIT_ERROR
