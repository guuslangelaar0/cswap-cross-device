"""Plugin hook handler: `cxd _hook <Event>` with the hook JSON on stdin.

Contract: fast, silent on error, exit 0 always. A broken hook must never
break the session it runs in.

SessionStart      register ~/.cxd/sessions/<sid>.json (pid, cwd, config dir)
PostToolUse       \
UserPromptSubmit   > claim queued inbox messages, return them as additionalContext
Stop              /  (Stop also appends last_assistant_message to the outbox)
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


def claim_inbox(sid):
    """Move queued messages to delivered/ one by one. os.rename is atomic, so
    when several hooks race (parallel tool calls) each message is claimed by
    exactly one of them."""
    inbox = path("inbox", sid)
    try:
        names = sorted(n for n in os.listdir(inbox) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []
    if not names:
        return []
    done = ensure_dir(path("delivered", sid))
    msgs, used = [], 0
    for n in names:
        src = os.path.join(inbox, n)
        try:
            size = os.path.getsize(src)
        except OSError:
            continue
        if msgs and used + size > CONTEXT_BUDGET:
            break  # leave the rest for the next hook
        dst = os.path.join(done, n)
        try:
            os.rename(src, dst)
        except OSError:
            continue  # another hook claimed it
        m = read_json(dst, None)
        if m:
            msgs.append(m)
            used += size
    return msgs


def _fmt_time(ts):
    return ts or now_iso()


def format_messages(msgs):
    parts = []
    for m in msgs:
        parts.append("Message from the lead session %s at %s: %s" % (
            m.get("sender") or "unknown", _fmt_time(m.get("ts")), m.get("text") or ""))
    text = "\n\n".join(parts)
    if len(text) > CONTEXT_BUDGET:
        text = text[: CONTEXT_BUDGET - 40] + "\n[message truncated by cxd]"
    return text


def handle(event, data):
    sid = data.get("session_id")
    if not isinstance(sid, str) or not SID_RE.match(sid):
        return None
    if event == "SessionStart":
        register(data)
    elif event == "SessionEnd":
        mark_ended(sid, data.get("reason"))
        return None
    ctx = None
    if event in ("SessionStart", "PostToolUse", "UserPromptSubmit", "Stop"):
        msgs = claim_inbox(sid)
        if msgs:
            ctx = format_messages(msgs)
            ensure_dir(path("outbox"))
            for m in msgs:
                append_jsonl(path("outbox", sid + ".jsonl"), {
                    "ts": now_iso(), "kind": "delivered", "session_id": sid, "id": m.get("id"),
                    "event": event, "text": (m.get("text") or "")[:200]})
    if event == "Stop":
        ensure_dir(path("outbox"))
        last = data.get("last_assistant_message")
        if isinstance(last, (dict, list)):
            last = json.dumps(last)
        append_jsonl(path("outbox", sid + ".jsonl"), {
            "ts": now_iso(), "kind": "stop", "session_id": sid,
            "text": (last or "")[:OUTBOX_TEXT], "stop_hook_active": bool(data.get("stop_hook_active"))})
    if ctx:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": ctx}}
    return None


def main(event):
    out = None
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            out = handle(event, data)
    except Exception:
        out = None
    if out:
        try:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
        except Exception:
            pass
    return 0
