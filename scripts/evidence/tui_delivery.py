#!/usr/bin/env python3
"""Evidence driver: hook-inbox delivery into an INTERACTIVE Claude Code (TUI)
session, driven in tmux. Real Claude, real hooks, tiny prompts.

    python3 scripts/evidence/tui_delivery.py --account <cswap-account> \
        [--plugin-dir DIR] [--model haiku] [--tmux PATH] --out docs/evidence/<log>.md

It starts `cswap run <account> -- [--plugin-dir DIR] --model M` in a private tmux
server (its own socket, no user tmux.conf) with a throwaway CXD_HOME and a
scrubbed environment, then proves, one case at a time:

  A  PostToolUse       a message queued while a Bash tool call runs
  B  UserPromptSubmit  a message queued while idle, then a prompt is typed
  C  Stop              a message queued while a tool-free answer streams; does
                       the session continue and act on it?
  D  idle              a message queued while nobody types after a turn: the
                       waker its Stop started (an asyncRewake hook) delivers it
                       and Claude acts on it unprompted
  E  untouched         a message to a session nobody has typed into (after
                       /clear): the waker its SessionStart started delivers it
  F  Esc               a message after a turn interrupted with Esc (no Stop, no
                       idle notification): it waits for the next prompt

A-C run with the waker off ("idle_wake": false), so they prove the hooks; D
and E turn it on.

A case passes only on cxd's own receipt naming the expected hook AND Claude's
reply (from the session transcript) following the message. Between cases the
session is brought back to idle with an empty inbox. The log is redacted (no
session ids, paths, host, user or account names) and is written even when a
case or the start fails. claude and the private tmux server are stopped at the
end, also on SIGTERM; the recorded claude pid is checked to be gone.
"""

import argparse
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CXD = os.path.join(REPO, "bin", "cxd")
sys.path.insert(0, os.path.join(REPO, "lib"))
from cxd.common import proc_alive, signal_verified, wait_dead, worker_env  # noqa: E402

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# cswap's per-account config directories look like "<n>-<user>_<domain>".
ACCOUNT_DIR = re.compile(r"\b\d+-[A-Za-z0-9._%+-]+_[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+\b")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Driver(object):
    def __init__(self, account, plugin_dir, model, tmux, tmp):
        self.account, self.plugin_dir, self.model, self.tmux_bin = account, plugin_dir, model, tmux
        self.tmp = tmp
        self.home = os.path.join(self.tmp, "cxd")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.home, mode=0o700)
        os.makedirs(self.work)
        # A machine where cxd is set up has a config. The hook paths (A-C) are
        # proven with the idle waker off; case D turns it on.
        self.set_config(idle_wake=False)
        self.sock = "cxdtui-%d" % os.getpid()
        self.session = "tui"
        # A neutral sender label: the injected message names it, and replies may quote it.
        self.env = worker_env({"CXD_HOME": self.home, "CXD_SELF": "laptop"})
        for k in ("TMUX", "CXD_RUN"):  # CXD_RUN: when the driver itself runs inside a cxd run
            self.env.pop(k, None)
        self.log = []
        self.t0 = time.time()
        self.sid = None
        self.transcript = None
        self.claude = (None, None)
        self.trust_answered = False
        self.prompts = 0  # prompts typed; each ends with at least one Stop record
        home = os.path.expanduser("~")
        try:  # this machine's and its peers' cxd names are host names too
            from cxd.common import load_config
            cfg = load_config()
            hostnames = list(cfg.get("self") or []) + list(cfg.get("hosts") or {})
        except Exception:
            hostnames = []
        self.redactions = [(h, "<host>") for h in hostnames if isinstance(h, str) and len(h) > 2]
        self.redactions += [(p, r) for p, r in (
            (self.tmp, "<tmp>"), (os.path.realpath(self.tmp), "<tmp>"),
            (plugin_dir or "", "<plugin-dir>"), (home, "~"),
            (socket.gethostname(), "<host>"), (socket.gethostname().split(".")[0], "<host>"),
            (getpass.getuser(), "<user>"), (account, "<account>"),
        ) if p and len(p) > 2]

    def set_config(self, **kw):
        p = os.path.join(self.home, "config.json")
        try:
            with open(p) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {"self": ["laptop"]}
        cfg.update(kw)
        with open(p, "w") as f:
            json.dump(cfg, f)

    # ------------------------------------------------------------ plumbing
    def redact(self, s):
        s = str(s)
        # Patterns first: a literal replacement inside them would hide them.
        s = UUID.sub("<uuid>", s)
        s = EMAIL.sub("<email>", s)
        s = ACCOUNT_DIR.sub("<account-dir>", s)
        for old, new in sorted(self.redactions, key=lambda x: -len(x[0])):
            s = re.sub(re.escape(old), lambda _m: new, s, flags=re.IGNORECASE)
        return re.sub(r"/(home|Users)/[^/\s\"']+", "~", s)

    def note(self, text):
        line = self.redact("[t+%5.1fs] %s" % (time.time() - self.t0, text))
        print(line, flush=True)
        self.log.append(line)

    def tmux(self, *args, check=True):
        r = subprocess.run([self.tmux_bin, "-L", self.sock, "-f", os.devnull] + list(args), env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if check and r.returncode != 0:
            raise RuntimeError("tmux %s: %s" % (args[0], r.stderr.strip()))
        return r.stdout

    def pane(self):
        return self.tmux("capture-pane", "-p", "-t", self.session, check=False)

    def keys(self, *keys):
        self.tmux("send-keys", "-t", self.session, *keys)

    def type_prompt(self, text):
        if not text.startswith("/"):
            self.prompts += 1
        self.tmux("send-keys", "-t", self.session, "-l", text)
        time.sleep(0.5)
        self.keys("Enter")

    def cxd(self, *args):
        r = subprocess.run([sys.executable, CXD] + list(args), env=self.env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def wait(self, pred, timeout, what, step=0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = pred()
            if v:
                return v
            time.sleep(step)
        tail = "\n".join(l for l in self.pane().splitlines() if l.strip())[-1500:]
        raise RuntimeError("timed out after %ss waiting for %s; screen:\n%s" % (timeout, what, self.redact(tail)))

    # ------------------------------------------------------------ session state
    def record(self):
        d = os.path.join(self.home, "sessions")
        try:
            names = [n for n in os.listdir(d) if n.endswith(".json")]
        except OSError:
            return None
        for n in names:
            try:
                with open(os.path.join(d, n)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue  # being written
            if rec.get("cwd") and os.path.realpath(rec["cwd"]) == os.path.realpath(self.work):
                return rec
        return None

    def outbox(self):
        out = []
        try:
            with open(os.path.join(self.home, "outbox", self.sid + ".jsonl")) as f:
                for l in f:
                    try:
                        out.append(json.loads(l))
                    except ValueError:
                        pass  # a line being written
        except OSError:
            pass
        return out

    def receipts(self, msg_id):
        return [r for r in self.outbox() if r.get("kind") == "delivered" and r.get("id") == msg_id]

    def inbox_pending(self):
        n = 0
        for box in ("inbox", "claimed"):
            try:
                n += len([x for x in os.listdir(os.path.join(self.home, box, self.sid)) if x.endswith(".json")])
            except OSError:
                pass
        return n

    def assistant_texts(self):
        out = []
        try:
            with open(self.transcript) as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") != "assistant":
                        continue
                    for block in (ev.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                            out.append(block["text"])
        except (OSError, TypeError):
            pass
        return out

    def stop_count(self):
        return len([r for r in self.outbox() if r.get("kind") == "stop"])

    def _activity(self):
        """Newest change to the transcript or the outbox (the session's own traces)."""
        t = 0.0
        for p in (self.transcript, os.path.join(self.home, "outbox", (self.sid or "-") + ".jsonl")):
            try:
                t = max(t, os.stat(p).st_mtime)
            except (OSError, TypeError):
                pass
        return t

    def idle(self, quiet=2.0):
        """Every prompt typed so far has ended its turn (a Stop record each), the
        input prompt is on screen, and the session has written nothing for
        `quiet` seconds. The input box stays on screen while Claude works, so the
        Stop count is what makes it safe to type (never into a running turn or a
        permission dialog)."""
        if self.stop_count() < self.prompts:
            return False
        lines = [l.strip() for l in self.pane().splitlines()]
        if not any(l.startswith("❯") for l in lines):
            return False
        return time.time() - self._activity() >= quiet

    def settle(self):
        """Back to a known state between cases: idle, and an empty inbox."""
        self.wait(self.idle, 120, "the session to go idle", step=1)
        if self.inbox_pending():
            self.note("settle: %d message(s) left over; draining with a prompt" % self.inbox_pending())
            before = self.stop_count()
            self.type_prompt("Reply with the single word OK.")
            self.wait(lambda: self.stop_count() > before and not self.inbox_pending() and self.idle(), 120,
                      "the leftover message to be delivered", step=1)

    # ------------------------------------------------------------ start / stop
    def start(self):
        cmd = ["cswap", "run", self.account, "--"]
        if self.plugin_dir:
            cmd += ["--plugin-dir", self.plugin_dir]
        cmd += ["--model", self.model, "--allowedTools", "Bash(sleep *)"]
        self.tmux("new-session", "-d", "-s", self.session, "-x", "200", "-y", "50", "-c", self.work,
                  " ".join(shlex.quote(c) for c in cmd))
        self.note("started claude TUI in a private tmux server: cswap run <account> -- %s" % " ".join(cmd[4:]))
        deadline = time.time() + 90
        while time.time() < deadline:
            pane = self.pane()
            if not self.trust_answered and "trust this folder" in pane.lower() and "enter to confirm" in pane.lower():
                # The default choice is "No, exit". Move to "Yes, I trust this folder"
                # and press Enter only once the screen shows it selected (the
                # dialog can be drawn before it takes keys).
                if re.search(r"❯\s*(\d\.\s*)?Yes, I trust this folder", pane):
                    self.note("workspace trust dialog shown; choosing 'Yes, I trust this folder'")
                    self.keys("Enter")
                    self.trust_answered = True
                else:
                    self.keys("Down")
                time.sleep(0.5)
                continue
            rec = self.record()
            if rec:
                self.sid = rec["session_id"]
                self.transcript = rec.get("transcript_path")
                self.claude = (rec.get("pid"), rec.get("pid_ident"))
                self.redactions.append((self.sid, "<sid>"))
                break
            time.sleep(1)
        if not self.sid:
            self.wait(lambda: False, 1, "the SessionStart hook to register the session")
        self.note("SessionStart hook registered the session (pid recorded: %s)" % bool(self.claude[0]))
        self.wait(self.idle, 60, "the input prompt")
        rc, out, _ = self.cxd("list", "local", "--json")
        listed = [s for s in json.loads(out)[0]["sessions"] if s["session_id"] == self.sid] if rc == 0 else []
        self.note("cxd list shows the session alive=%s" % (listed[0]["alive"] if listed else None))

    def stop(self):
        try:
            if self.sid and self.idle():
                self.type_prompt("/exit")
                self.wait(lambda: (self.record() or {}).get("ended"), 30, "SessionEnd", step=1)
                self.note("/exit -> SessionEnd hook marked the session ended (reason=%s)" % self.record().get("end_reason"))
        except Exception as e:
            self.note("stop: %s" % e)
        try:
            self.tmux("kill-server", check=False)
        except Exception as e:
            self.note("stop: tmux kill-server: %s" % e)
        pid, ident = self.claude
        if not (pid and ident):
            self.note("claude pid or identity was not recorded: cannot verify it exited")
            return
        if wait_dead(pid, ident, 10):
            self.note("claude process is gone")
            return
        # Still ours (identity checked): end it, and escalate if it ignores SIGTERM.
        for sig, wait_s in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            signal_verified(pid, ident, sig)
            if wait_dead(pid, ident, wait_s):
                self.note("claude outlived its tmux server; ended it with %s" % signal.Signals(sig).name)
                return
        self.note("WARNING: claude pid %s is still alive after SIGKILL" % pid)

    # ------------------------------------------------------------ cases
    def send(self, text):
        rc, out, err = self.cxd("send", "local", self.sid, text, "--json")
        if rc != 0:
            raise RuntimeError("cxd send failed rc=%s %s" % (rc, err))
        res = json.loads(out)
        self.note("cxd send -> via %s" % res["via"])
        return res["id"]

    def reply_with(self, marker, after):
        return [t for t in self.assistant_texts()[after:] if marker in t]

    def case(self, title, fn):
        self.note("=== %s" % title)
        try:
            verdict = fn()
        except Exception as e:
            verdict = "FAIL: %s" % e
        self.note("verdict: %s" % verdict)
        try:
            self.settle()
        except Exception as e:
            self.note("settle failed: %s" % e)
        return verdict

    def case_post_tool_use(self):
        before, n0 = self.stop_count(), len(self.assistant_texts())
        self.type_prompt("Use the Bash tool to run exactly: sleep 12 ; then reply with the single word DONE.")
        time.sleep(5)  # past UserPromptSubmit; the sleep is running
        mid = self.send("Reply also with the word MANGO-A. (cxd evidence check)")
        rec = self.wait(lambda: self.receipts(mid), 60, "the delivery receipt", step=1)[0]
        self.note("receipt: delivered by the %s hook" % rec["event"])
        texts = self.wait(lambda: self.reply_with("MANGO-A", n0), 120, "a reply with the marker", step=1)
        self.wait(lambda: self.stop_count() > before and self.idle(), 120, "the turn to end", step=1)
        self.note("Claude replied: %r" % texts[-1][:200])
        return "PASS" if rec["event"] == "PostToolUse" else "FAIL: delivered via %s, not PostToolUse" % rec["event"]

    def case_user_prompt_submit(self):
        before, n0 = self.stop_count(), len(self.assistant_texts())
        mid = self.send("Reply also with the word KIWI-B. (cxd evidence check)")
        time.sleep(3)
        self.note("while idle, %d message(s) wait in the inbox" % self.inbox_pending())
        self.type_prompt("Reply with the single word READY.")
        rec = self.wait(lambda: self.receipts(mid), 60, "the delivery receipt", step=1)[0]
        self.note("receipt: delivered by the %s hook" % rec["event"])
        texts = self.wait(lambda: self.reply_with("KIWI-B", n0), 120, "a reply with the marker", step=1)
        self.wait(lambda: self.stop_count() > before and self.idle(), 120, "the turn to end", step=1)
        stops = self.stop_count() - before
        self.note("Claude replied: %r; Stop hook records this turn: %d" % (texts[-1][:200], stops))
        if stops != 1:
            return "FAIL: %d Stop records for one turn (is the plugin loaded twice?)" % stops
        return "PASS" if rec["event"] == "UserPromptSubmit" else "FAIL: delivered via %s" % rec["event"]

    def case_stop(self, attempts=3):
        for attempt in range(1, attempts + 1):
            before, n0 = self.stop_count(), len(self.assistant_texts())
            marker = "LIME-C%d" % attempt  # unique per attempt: an earlier reply cannot count
            self.type_prompt("Without using any tools, write thirty numbered lines, each a short sentence about the sea.")
            time.sleep(2.0)  # past UserPromptSubmit, while the answer is (normally) still being written
            mid = self.send("Reply also with the word %s. (cxd evidence check)" % marker)
            try:
                rec = self.wait(lambda: self.receipts(mid), 45, "the delivery receipt", step=1)[0]
            except RuntimeError:
                rec = None
            if rec and rec["event"] == "Stop":
                break
            self.note("attempt %d: %s; trying again" % (attempt, "the answer ended before the send" if not rec
                                                        else "delivered via %s" % rec["event"]))
            self.settle()
        else:
            return "INCONCLUSIVE: no attempt delivered at Stop"
        self.note("receipt: delivered by the %s hook" % rec["event"])
        # The delivering Stop run writes its receipt, then its own Stop record.
        self.wait(lambda: self.stop_count() > before, 30, "the delivering Stop record", step=0.5)
        stop_seen = before + 1
        texts = self.wait(lambda: self.reply_with(marker, n0), 120, "a follow-up with the marker", step=1)
        self.wait(lambda: self.stop_count() > stop_seen and self.idle(), 120, "the continued turn to end", step=1)
        n_new = len(self.assistant_texts()) - n0
        self.note("assistant text blocks this case: %d; Stop hook runs this case: %d" % (n_new, self.stop_count() - before))
        self.note("Claude's follow-up: %r" % texts[-1][:200])
        # The marker is in a block written after the delivering Stop run, and another Stop followed.
        return "PASS: delivered at Stop; the session continued and acted on it"

    def case_idle(self, wait_s):
        """With the waker (an asyncRewake hook) the message arrives while the
        session sits idle, and Claude acts on it with no prompt typed. Without
        it, the message waits for the next prompt."""
        self.set_config(idle_wake=True)
        before = self.stop_count()
        self.type_prompt("Reply with the single word ARMED.")  # its Stop starts a waker
        self.wait(lambda: self.stop_count() > before and self.idle(), 120, "the arming turn to end", step=1)
        time.sleep(2)
        n0, prompts0 = len(self.assistant_texts()), self.prompts
        mid = self.send("Reply also with the word PLUM-D. (cxd evidence check)")
        deadline = time.time() + wait_s
        while time.time() < deadline and not self.receipts(mid):
            time.sleep(1)
        got = self.receipts(mid)
        if got:
            self.note("receipt: delivered by the %s hook while idle (no prompt typed)" % got[0]["event"])
            texts = self.wait(lambda: self.reply_with("PLUM-D", n0), 120, "Claude's reply to the woken message", step=1)
            self.note("Claude replied, unprompted: %r" % texts[-1][:200])
            if self.prompts != prompts0:
                return "FAIL: a prompt was typed during the case"
            if got[0]["event"] != "Wake":
                return "FAIL: delivered while idle via %s, not the waker" % got[0]["event"]
            return "PASS: delivered while idle by the waker; Claude acted on it without a prompt"
        self.note("after %ds idle: not delivered, still queued (%d pending)" % (wait_s, self.inbox_pending()))
        self.type_prompt("Reply with the single word OK.")  # hand it over, so the next case starts clean
        self.wait(lambda: self.receipts(mid), 60, "delivery on the next prompt", step=1)
        return "FAIL: not delivered while idle within %ds (the next prompt delivered it)" % wait_s

    def case_esc(self, wait_s):
        """A turn interrupted with Esc ends without a Stop and without Claude
        Code's idle notification, so the session counts as busy: a message
        sent then waits for the next prompt (a documented limitation), and
        that prompt delivers it."""
        self.set_config(idle_wake=True)
        for attempt in range(1, 4):
            n0 = len(self.assistant_texts())
            stops = self.stop_count()
            self.type_prompt("Write the numbers from 1 to 5000, one per line, nothing else.")
            self.wait(lambda: self._streaming(n0), 60, "the answer to start streaming", step=0.1)
            self.keys("Escape")
            # Wait until the turn is over (the spinner is gone), then judge by
            # the Stop record: an interrupted turn writes none.
            self.wait(lambda: self._turn_over(), 150, "the turn to end after Esc", step=0.5)
            time.sleep(3)
            if self.stop_count() == stops:
                self.prompts -= 1
                break
            self.note("attempt %d: the answer ended (with a Stop) before Esc reached it; again" % attempt)
            self.settle()
        else:
            return "FAIL: no turn could be interrupted in 3 attempts"
        self.note("pressed Esc while the answer streamed; the turn ended without a Stop record (interrupted)")
        n1 = len(self.assistant_texts())
        mid = self.send("Reply with the word PEAR-F. (cxd evidence check)")
        deadline = time.time() + wait_s
        while time.time() < deadline and not self.receipts(mid):
            time.sleep(1)
        if self.receipts(mid):
            return "FAIL: delivered while the session counted as busy (via %s)" % self.receipts(mid)[0]["event"]
        self.note("after %ds: not delivered (the session counts as busy after Esc), still queued" % wait_s)
        self.type_prompt("Continue.")  # neutral: the delivered message says what to do
        rec = self.wait(lambda: self.receipts(mid), 60, "delivery on the next prompt", step=1)[0]
        texts = self.wait(lambda: self.reply_with("PEAR-F", n1), 120, "a reply with the marker", step=1)
        self.note("the next prompt delivered it (receipt: %s hook); Claude replied: %r" % (rec["event"], texts[-1][:120]))
        if rec["event"] != "UserPromptSubmit":
            return "FAIL: the follow-up delivery came via %s" % rec["event"]
        return "PASS: after Esc the message waited for the next prompt, which delivered it (documented limitation)"

    def _streaming(self, n0):
        """The answer is streaming: numbers on screen, or the spinner counting
        tokens down ("(12s · ↓ 1.2k tokens)"), which is all Claude Code shows of
        a long answer until it is done."""
        pane = self.pane()
        return (len(re.findall(r"(?m)^\s*\d+\s*$", pane)) >= 3 or bool(re.search(r"\u2193 [\d.]+k? tokens", pane))
                or len(self.assistant_texts()) > n0)

    def _turn_over(self, quiet=2.0):
        """No spinner on screen for `quiet` seconds."""
        deadline = time.time() + quiet
        while time.time() < deadline:
            if re.search(r"\u2193 [\d.]+k? tokens|\(\d+s \u00b7", self.pane()):
                return False
            time.sleep(0.3)
        return True

    def _new_record(self, old_sid):
        d = os.path.join(self.home, "sessions")
        for n in os.listdir(d) if os.path.isdir(d) else []:
            try:
                with open(os.path.join(d, n)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if rec.get("session_id") != old_sid and not rec.get("ended") and rec.get("cwd") \
                    and os.path.realpath(rec["cwd"]) == os.path.realpath(self.work):
                return rec
        return None

    def case_session_start_waker(self, wait_s):
        """A session nobody has typed into yet (here the one /clear starts): the
        waker its SessionStart hook started delivers the message, and Claude
        acts on it with no prompt typed. In real use this waker, holding the
        session's lock, is the one that delivers."""
        self.set_config(idle_wake=True)
        old = self.sid
        self.type_prompt("/clear")
        rec = self.wait(lambda: self._new_record(old), 60, "the session /clear starts", step=1)
        self.sid, self.transcript = rec["session_id"], rec.get("transcript_path")
        self.claude = (rec.get("pid"), rec.get("pid_ident"))
        self.redactions.append((self.sid, "<sid2>"))
        self.prompts = 0  # Stop records are counted per session
        self.note("/clear -> SessionEnd for the old session, SessionStart registered the new one")
        time.sleep(3)
        n0 = len(self.assistant_texts())
        mid = self.send("Reply with the word FIG-E. (cxd evidence check)")
        deadline = time.time() + wait_s
        while time.time() < deadline and not self.receipts(mid):
            time.sleep(1)
        got = self.receipts(mid)
        if not got:
            self.type_prompt("Reply with the single word OK.")
            return "FAIL: not delivered within %ds by the SessionStart waker" % wait_s
        self.note("receipt: delivered by the %s hook; no prompt has been typed in this session" % got[0]["event"])
        texts = self.wait(lambda: self.reply_with("FIG-E", n0), 120, "Claude's reply to the woken message", step=1)
        self.note("Claude replied, unprompted: %r" % texts[-1][:200])
        if got[0]["event"] != "Wake" or self.prompts:
            return "FAIL: delivered via %s with %d prompt(s) typed" % (got[0]["event"], self.prompts)
        return "PASS: the SessionStart waker delivered to an untouched session; Claude acted on it"


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--plugin-dir")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--tmux", default=shutil.which("tmux") or "tmux")
    ap.add_argument("--idle-wait", type=int, default=30)
    ap.add_argument("--cases", default="ABCDEF")
    ap.add_argument("--out")
    ap.add_argument("--keep-transcript", help="copy the session transcript here (for debugging; not redacted)")
    a = ap.parse_args(argv)
    cases = [c for c in a.cases.upper() if c in "ABCDEF"]
    if not cases:
        sys.stderr.write("no case selected (use --cases from ABCDEF)\n")
        return 2
    plugin_dir = a.plugin_dir and os.path.abspath(a.plugin_dir)
    tmp = tempfile.mkdtemp(prefix="cxd-tui-")
    d = Driver(a.account, plugin_dir, a.model, a.tmux, tmp)
    for sig in (signal.SIGTERM, signal.SIGHUP):  # run the finally blocks
        signal.signal(sig, lambda *_: sys.exit(143))
    results = {}
    try:
        ver = subprocess.run(["cswap", "run", a.account, "--", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, env=d.env).stdout
        ver = [l for l in ver.splitlines() if "Claude Code" in l] or ["unknown"]
        cxdv = subprocess.run([sys.executable, CXD, "--version"], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        tree = plugin_dir or REPO
        commit = subprocess.run(["git", "-C", tree, "rev-parse", "--short=12", "HEAD"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip() or "unknown"
        dirty = subprocess.run(["git", "-C", tree, "status", "--porcelain", "--", "lib", "hooks", "bin", "scripts/evidence"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout.strip()
        d.note("Claude Code %s (as launched by cswap); %s; plugin tree at %s%s; %s %s; python %s; model %s" % (
            ver[0].split(" (")[0], cxdv, commit, " (with local changes)" if dirty else "", platform.system(),
            platform.machine(), platform.python_version(), a.model))
        d.start()
        labels = {"A": "A: delivery on PostToolUse", "B": "B: delivery on UserPromptSubmit",
                  "C": "C: delivery at Stop, and whether the session continues",
                  "D": "D: a message queued while the session is idle (the waker a Stop started)",
                  "E": "E: a message to a session nobody has typed into (the waker SessionStart started)",
                  "F": "F: a message after a turn interrupted with Esc (waits for the next prompt)"}
        fns = {"A": d.case_post_tool_use, "B": d.case_user_prompt_submit, "C": d.case_stop,
               "D": lambda: d.case_idle(a.idle_wait), "E": lambda: d.case_session_start_waker(a.idle_wait),
               "F": lambda: d.case_esc(a.idle_wait)}
        for c in cases:
            results[labels[c].split(":")[0]] = d.case(labels[c], fns[c])
    except Exception as e:
        d.note("ABORTED: %s" % e)
        results["start"] = "FAIL: %s" % d.redact(e)
    finally:
        d.stop()
        if a.keep_transcript and d.transcript and os.path.exists(d.transcript):
            shutil.copy(d.transcript, a.keep_transcript)
        shutil.rmtree(tmp, ignore_errors=True)
        d.note("summary: " + json.dumps(results))
        if a.out:
            with open(a.out, "w") as f:
                f.write("```\n" + "\n".join(d.log) + "\n```\n")
    ok = bool(results) and all(str(v).startswith("PASS") for v in results.values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
