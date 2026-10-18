#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json ...`.

Reads stream-json user messages from stdin, one turn per message, and writes
stream-json events like the real CLI: init (once), the replayed user message,
an assistant message and a result. Message text controls it:
  SLEEP <s>   simulate <s> seconds of foreground work in that turn
  MARK-<x>    echoed into the result, so tests can see what was acted on
  PR          adds a pull-request URL to the assistant text
Env: STUB_IGNORE_EOF=1 keeps running after stdin EOF; STUB_IGNORE_INT=1
ignores SIGINT. SIGINT exits 130, SIGTERM 143 (like the real CLI).
"""
import json, os, re, signal, sys, time, uuid

args = sys.argv[1:]
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
if os.environ.get("STUB_FAIL_START"):
    sys.stderr.write("error: unknown option '--bogus'\n")
    sys.exit(1)


def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


signal.signal(signal.SIGINT, signal.SIG_IGN if os.environ.get("STUB_IGNORE_INT") else (lambda *_: os._exit(130)))
signal.signal(signal.SIGTERM, lambda *_: os._exit(143))

init = False
turns = 0
for line in iter(sys.stdin.readline, ""):
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    text = msg["message"]["content"]
    if not init:
        emit({"type": "system", "subtype": "init", "session_id": sid, "model": "stub",
              "resumed": "--resume" in args, "argv": args, "account": os.environ.get("STUB_ACCOUNT"),
              "cwd": os.getcwd(), "leaked_session_env": os.environ.get("CLAUDE_CODE_SESSION_ID")})
        init = True
    emit({"type": "user", "message": msg["message"], "session_id": sid})
    turns += 1
    m = re.search(r"SLEEP (\d+(?:\.\d+)?)", text)
    if m:
        time.sleep(float(m.group(1)))
    marks = re.findall(r"MARK-[A-Za-z0-9]+", text)
    body = "STUB-DONE " + " ".join(marks)
    if "PR" in text.split():
        body += " https://github.com/example/repo/pull/7"
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": body}]},
          "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
          "total_cost_usd": round(0.001 * turns, 6), "duration_ms": 1, "result": body, "session_id": sid})

if os.environ.get("STUB_IGNORE_EOF"):
    while True:
        time.sleep(1)
sys.exit(0)
