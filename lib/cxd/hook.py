"""Plugin hook handler: `cxd _hook <Event>` with the hook JSON on stdin.

Contract: fast, silent on error, exit 0 always. A broken hook must never
break the session it runs in.

SessionStart      register ~/.cxd/sessions/<sid>.json (pid, cwd, config dir)
PostToolUse       \
UserPromptSubmit   > claim queued inbox messages, return them as additionalContext,
Stop              /  ack them once written (Stop also records last_assistant_message)
SessionEnd        mark the session ended
"""

import json
import os
import sys
import time

from .common import (
    SID_RE, append_jsonl, atomic_write_json, ensure_dir, now_iso, path,
    proc_identity, proc_parent_and_args, read_json, self_label,
)

CONTEXT_BUDGET = 9000  # additionalContext is capped at 10k characters
OUTBOX_TEXT = 2000


def _claude_pid():
    """The nearest claude ancestor of this hook process. (CLAUDE_PID in the
    environment is not trusted: a session started from another session's Bash
    tool inherits the parent's value.)"""
    pid = os.getppid()
    for _ in range(6):
        if not pid or pid <= 1:
            return None
        ppid, args = proc_parent_and_args(pid)
        if args is None:
            return None
        first = args.split(" ", 1)[0]
        if os.path.basename(first) == "claude" or "/claude/versions/" in first:
            return pid
        pid = ppid
    return None


def _account_label():
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.basename(cfg_dir.rstrip("/")) if cfg_dir else "default"


def register(data, event_source=None):
    sid = data["session_id"]
    ensure_dir(path("sessions"))
    p = path("sessions", sid + ".json")
    rec = read_json(p, {}) or {}
    pid = _claude_pid()
    rec.update({
        "session_id": sid, "cwd": data.get("cwd"), "host": self_label(),
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude",
        "account": _account_label(), "pid": pid, "pid_ident": proc_identity(pid) if pid else None,
        "registered": now_iso(), "source": data.get("source") or event_source,
        "transcript_path": data.get("transcript_path"), "cxd_run": os.environ.get("CXD_RUN") or None,
        "ended": None,
    })
    atomic_write_json(p, rec)


def mark_ended(sid, reason):
    p = path("sessions", sid + ".json")
    rec = read_json(p, None)
    if rec is None:
        return
    rec["ended"] = now_iso()
    rec["end_reason"] = reason
    atomic_write_json(p, rec)


# At-least-once delivery. A hook claims a message by renaming it from inbox/
# to claimed/<sid>/<id>.claim-<pid>-<hash>.json, where <hash> fingerprints the
# hook process's start identity: os.rename is atomic, so racing hooks (parallel
# tool calls) each claim different messages, and a recycled pid cannot pass for
# the claimer. Only after the hook has written its output does it move the
# claim to delivered/ (the ack). Every hook of the session first returns claims
# whose claimer has died to the inbox, so a hook that is killed, times out or
# cannot write its output loses nothing; the message comes again.

def claim_token(pid=None):
    import hashlib
    pid = pid or os.getpid()
    ident = proc_identity(pid) or "unknown"
    return "%d-%s" % (pid, hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12])


def _claimer_alive(token):
    pid, _, digest = token.partition("-")
    try:
        pid = int(pid)
    except ValueError:
        return False
    if pid == os.getpid():
        return True
    return claim_token(pid) == "%d-%s" % (pid, digest)


def recover_claims(sid):
    """Put messages claimed by hooks that have died back in the inbox."""
    claimed = path("claimed", sid)
    try:
        names = os.listdir(claimed)
    except OSError:
        return
    inbox = None
    for n in names:
        mid, sep, rest = n.partition(".claim-")
        if not sep or not rest.endswith(".json") or _claimer_alive(rest[:-5]):
            continue
        inbox = inbox or ensure_dir(path("inbox", sid))
        try:
            os.rename(os.path.join(claimed, n), os.path.join(inbox, mid + ".json"))  # GUARD:redeliver
        except OSError:
            pass  # another hook requeued it first


def claim_inbox(sid):
    """Claim queued messages, oldest first, within the context budget.
    Returns [(message, claimed_path)]."""
    recover_claims(sid)
    inbox = path("inbox", sid)
    try:
        names = sorted(n for n in os.listdir(inbox) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []
    if not names:
        return []
    claimed = ensure_dir(path("claimed", sid))
    token = claim_token()
    got, used = [], 0
    for n in names:
        src = os.path.join(inbox, n)
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if got and used + size > CONTEXT_BUDGET:
            break  # leave the rest for the next hook
        dst = os.path.join(claimed, "%s.claim-%s.json" % (n[:-5], token))
        try:
            os.rename(src, dst)
        except OSError:
            continue  # another hook claimed it
        m = read_json(dst, None)
        if m:
            got.append((m, dst))
            used += size
    return got


def ack(sid, claims, event):
    """The hook's output is written: the claimed messages are delivered."""
    done = ensure_dir(path("delivered", sid))
    ensure_dir(path("outbox"))
    for m, claimed in claims:
        try:
            os.rename(claimed, os.path.join(done, os.path.basename(claimed).partition(".claim-")[0] + ".json"))
        except OSError:
            continue
        append_jsonl(path("outbox", sid + ".jsonl"), {
            "ts": now_iso(), "kind": "delivered", "session_id": sid, "id": m.get("id"),
            "event": event, "text": (m.get("text") or "")[:200]})


def _fmt_time(ts):
    return ts or now_iso()


def format_messages(msgs):
    parts = []
    for m in msgs:
        parts.append("Message from the lead session %s at %s (cxd message %s): %s" % (
            m.get("sender") or "unknown", _fmt_time(m.get("ts")), m.get("id") or "?", m.get("text") or ""))
    text = "\n\n".join(parts)
    if len(text) > CONTEXT_BUDGET:
        text = text[: CONTEXT_BUDGET - 40] + "\n[message truncated by cxd]"
    return text


def handle(event, data):
    """Returns (output or None, claims to ack once the output is written)."""
    sid = data.get("session_id")
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None, []
    if event == "SessionStart":
        register(data)
    elif event == "SessionEnd":
        mark_ended(sid, data.get("reason"))
        return None, []
    ctx, claims = None, []
    if event in ("SessionStart", "PostToolUse", "UserPromptSubmit", "Stop"):
        claims = claim_inbox(sid)
        if claims:
            ctx = format_messages([m for m, _ in claims])
    if event == "Stop":
        ensure_dir(path("outbox"))
        last = data.get("last_assistant_message")
        if isinstance(last, (dict, list)):
            last = json.dumps(last)
        append_jsonl(path("outbox", sid + ".jsonl"), {
            "ts": now_iso(), "kind": "stop", "session_id": sid,
            "text": (last or "")[:OUTBOX_TEXT], "stop_hook_active": bool(data.get("stop_hook_active"))})
    if ctx:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}, claims
    return None, claims


def main(event):
    out, claims, sid = None, [], None
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            sid = data.get("session_id")
            out, claims = handle(event, data)
    except Exception:
        out = None  # claims stay claimed by this process; the next hook requeues them
    if out:
        try:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
        except Exception:
            return 0  # not handed over: no ack, so the messages come again
        try:
            ack(sid, claims, event)  # GUARD:ack-after-write
        except Exception:
            pass
    return 0
