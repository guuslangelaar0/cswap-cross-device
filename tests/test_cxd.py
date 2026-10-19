"""Plumbing tests with a stub `claude` (tests/fakes/claude_stub.py), a fake
`cswap` and a fake `ssh`. Run: python3 -m unittest -v tests.test_cxd

Each guard test names the source mutation that must turn it red; see
tests/mutate.py, which applies each mutation to a throwaway copy and runs it.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CXD = os.path.join(REPO, "bin", "cxd")
HOOK = os.path.join(REPO, "hooks", "cxd-hook")
FAKES = os.path.join(REPO, "tests", "fakes")
sys.path.insert(0, os.path.join(REPO, "lib"))

from cxd.common import proc_alive, read_json  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxd-test-")
        self.local = os.path.join(self.tmp, "local", ".cxd")
        self.remote = os.path.join(self.tmp, "remote", ".cxd")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith(("CLAUDE_CODE_", "STUB_", "FAKE_SSH_")) or k in ("CXD_HOME", "CXD_SELF", "CXD_RUN"):
                del self.env[k]
        self.env.update({
            "CXD_HOME": self.local, "CXD_SELF": "laptop",
            "CXD_CSWAP": os.path.join(FAKES, "cswap"), "CXD_SSH": os.path.join(FAKES, "ssh"),
            # Never the real claude: a run without an account execs CXD_CLAUDE.
            "CXD_CLAUDE": os.path.join(FAKES, "claude_stub.py"),
            "FAKE_REMOTE_CXD_HOME": self.remote,
        })
        os.makedirs(self.local, mode=0o700)
        os.makedirs(self.remote, mode=0o700)
        # Runs go through (fake) `cswap run work --` unless a test says otherwise.
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump({"self": ["laptop"], "default_account": "work", "auto_prune": False,
                       "hosts": {"buildbox": {"transport": "ssh", "python": sys.executable, "cxd": CXD}}}, f)
        with open(os.path.join(self.remote, "config.json"), "w") as f:
            json.dump({"default_account": "work"}, f)
        self.bg = []

    def tearDown(self):
        for p in self.bg:
            if p.poll() is None:
                p.kill()
                p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                if f:
                    f.close()
        # Kill only processes this test's cxd recorded (verified identity).
        for home in (self.local, self.remote):
            rd = os.path.join(home, "runs")
            if not os.path.isdir(rd):
                continue
            for name in os.listdir(rd):
                pid_files = [f for f in os.listdir(os.path.join(rd, name)) if f.startswith("pids")]
                for pf in pid_files:
                    pids = read_json(os.path.join(rd, name, pf), {}) or {}
                    for k in ("claude", "keeper", "supervisor"):
                        if proc_alive(pids.get(k + "_pid"), pids.get(k + "_ident")):
                            try:
                                os.kill(pids[k + "_pid"], signal.SIGKILL)
                            except OSError:
                                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- helpers
    def cxd(self, *args, env=None, timeout=120, stdin=None):
        e = dict(self.env)
        if env:
            e.update(env)
        r = subprocess.run([sys.executable, CXD] + list(args), input=stdin, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=e, timeout=timeout,
                           cwd=self.work)
        return r

    def ok(self, *args, **kw):
        r = self.cxd(*args, **kw)
        self.assertEqual(r.returncode, 0, "cxd %s failed rc=%s\nstdout=%s\nstderr=%s" % (args, r.returncode, r.stdout, r.stderr))
        return r

    def prompt(self, text):
        p = os.path.join(self.tmp, "prompt-%d.txt" % time.time_ns())
        with open(p, "w") as f:
            f.write(text)
        return p

    def home(self, host):
        return self.local if host == "laptop" else self.remote

    def pids(self, host, name):
        return read_json(os.path.join(self.home(host), "runs", name, "pids.json"), {}) or {}

    def results(self, host, name):
        out = []
        p = os.path.join(self.home(host), "runs", name, "out.jsonl")
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        ev = json.loads(line)
                        if ev.get("type") == "result":
                            out.append(ev["result"])
        except OSError:
            pass
        return out

    def inits(self, host, name):
        """The stub's init event of every gen: it records the argv claude got."""
        with open(os.path.join(self.home(host), "runs", name, "out.jsonl")) as f:
            return [json.loads(l) for l in f if '"init"' in l]

    def wait_results(self, host, name, n, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.results(host, name)
            if len(res) >= n:
                return res
            time.sleep(0.1)
        self.fail("run %s/%s produced %d results, wanted %d" % (host, name, len(self.results(host, name)), n))

    def start(self, host, name, text, env=None):
        r = self.ok("start", host, name, self.prompt(text), "--init-timeout", "20", env=env)
        self.assertIn("started", r.stdout)
        return r

    def watch_bg(self, host, name, env=None):
        e = dict(self.env)
        if env:
            e.update(env)
        log = os.path.join(self.tmp, "watch-%s-%s.log" % (host, name))
        with open(log, "w") as out:
            p = subprocess.Popen([sys.executable, CXD, "watch", host, name, "--json", "--heartbeat", "3600"],
                                 stdout=out, stderr=subprocess.STDOUT, env=e, universal_newlines=True)
        self.bg.append(p)
        return p, log

    def log_text(self, log):
        with open(log) as f:
            return f.read()

    def read_events(self, log):
        evs = []
        with open(log) as f:
            for line in f:
                if not line.endswith("\n"):
                    break  # still being written; the next read sees it whole
                line = line.strip()
                if line.startswith("{"):
                    evs.append(json.loads(line))
        return evs

    def wait_event(self, log, pred, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in self.read_events(log):
                if pred(e):
                    return e
            time.sleep(0.1)
        self.fail("no matching event in %s:\n%s" % (log, self.log_text(log)))


class TestRuns(Base):
    def test_start_send_list(self):
        self.start("laptop", "w1", "hello MARK-A")
        res = self.wait_results("laptop", "w1", 1)
        self.assertIn("MARK-A", res[0])
        self.ok("send", "laptop", "w1", "second MARK-B")
        res = self.wait_results("laptop", "w1", 2)
        self.assertIn("MARK-B", res[1])
        r = self.ok("list", "laptop", "--json")
        runs = json.loads(r.stdout)[0]["runs"]
        self.assertEqual([(x["name"], x["alive"], x["turns"]) for x in runs], [("w1", True, 2)])
        self.assertIn("MARK-B", runs[0]["last_result"])
        self.assertAlmostEqual(runs[0]["cost_usd"], 0.002, places=6)
        mode = os.stat(os.path.join(self.local, "runs", "w1")).st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_keeper_holds_session_open_between_messages(self):
        """GUARD keeper. Mutation: supervisor starts no keeper -> EOF after the
        first message ends the session."""
        self.start("laptop", "w2", "first MARK-1")
        self.wait_results("laptop", "w2", 1)
        time.sleep(1.5)
        pids = self.pids("laptop", "w2")
        self.assertTrue(proc_alive(pids["claude_pid"], pids["claude_ident"]), "session died between messages")
        self.ok("send", "laptop", "w2", "MARK-2")
        self.assertIn("MARK-2", self.wait_results("laptop", "w2", 2)[1])

    def test_mid_run_send_is_acted_on(self):
        self.start("laptop", "w3", "SLEEP 3 MARK-FIRST")
        time.sleep(0.5)
        self.assertEqual(self.results("laptop", "w3"), [])  # still in turn 1
        self.ok("send", "laptop", "w3", "also MARK-MID")
        res = self.wait_results("laptop", "w3", 2)
        self.assertIn("MARK-FIRST", res[0])
        self.assertIn("MARK-MID", res[1])

    def test_send_to_dead_run_is_refused(self):
        """GUARD send-dead. Mutation: liveness check removed AND the FIFO opened
        O_RDWR (a writer that never fails) -> the send 'succeeds' silently."""
        self.start("laptop", "w4", "MARK-X")
        self.wait_results("laptop", "w4", 1)
        self.ok("stop", "laptop", "w4", "--grace", "10")
        r = self.cxd("send", "laptop", "w4", "MARK-LATE")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("not running", r.stderr)
        self.assertNotIn("MARK-LATE", " ".join(self.results("laptop", "w4")))

    def test_stop_by_eof(self):
        """GUARD stop-alive. Mutation: signals are never delivered -> stop
        leaves the process alive."""
        self.start("laptop", "w5", "MARK-S")
        self.wait_results("laptop", "w5", 1)
        pids = self.pids("laptop", "w5")
        r = self.ok("stop", "laptop", "w5", "--grace", "10", "--int-wait", "2", "--term-wait", "2", "--json")
        res = json.loads(r.stdout)
        self.assertTrue(res["stopped"])
        self.assertEqual(res["steps"], ["keeper-eof", "exited-on-eof"])
        self.assertEqual(res["rc"], 0)
        self.assertFalse(proc_alive(pids["claude_pid"], pids["claude_ident"]))
        self.assertFalse(proc_alive(pids["keeper_pid"], pids["keeper_ident"]))

    def test_stop_escalates_to_sigint_and_sigterm(self):
        """GUARD stop-alive (escalation path)."""
        self.start("laptop", "w6", "MARK-S", env={"STUB_IGNORE_EOF": "1", "STUB_IGNORE_INT": "1"})
        self.wait_results("laptop", "w6", 1)
        pids = self.pids("laptop", "w6")
        r = self.ok("stop", "laptop", "w6", "--grace", "1", "--int-wait", "1", "--term-wait", "5", "--json")
        res = json.loads(r.stdout)
        self.assertEqual(res["steps"], ["keeper-eof", "grace-expired", "sigint", "sigterm"])
        self.assertEqual(res["rc"], 143)  # SIGTERM, as the real CLI reports it
        self.assertFalse(proc_alive(pids["claude_pid"], pids["claude_ident"]))

    def test_stop_when_eof_is_ignored_interrupts_once_idle(self):
        """macOS behaviour: claude does not exit on FIFO EOF. stop must not sit
        out the whole grace period on an idle run."""
        self.start("laptop", "wi", "MARK-1", env={"STUB_IGNORE_EOF": "1"})
        self.wait_results("laptop", "wi", 1)
        t0 = time.time()
        r = self.ok("stop", "laptop", "wi", "--grace", "60", "--json")
        res = json.loads(r.stdout)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(res["steps"], ["keeper-eof", "turn-idle", "sigint"])

    def test_stop_waits_for_the_running_turn(self):
        self.start("laptop", "wt", "SLEEP 4 MARK-FINISHED", env={"STUB_IGNORE_EOF": "1"})
        time.sleep(0.5)
        self.assertEqual(self.results("laptop", "wt"), [])
        r = self.ok("stop", "laptop", "wt", "--grace", "60", "--json")
        res = json.loads(r.stdout)
        self.assertEqual(res["steps"], ["keeper-eof", "turn-idle", "sigint"])
        self.assertIn("MARK-FINISHED", self.results("laptop", "wt")[0])  # the turn completed

    def test_resume_refused_while_alive(self):
        """GUARD resume-alive. Mutation: the alive check in resume is removed."""
        self.start("laptop", "w7", "MARK-1")
        self.wait_results("laptop", "w7", 1)
        before = self.pids("laptop", "w7")
        r = self.cxd("resume", "laptop", "w7", self.prompt("MARK-2"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("still running", r.stderr)
        after = self.pids("laptop", "w7")
        self.assertEqual(before["claude_pid"], after["claude_pid"])
        self.assertTrue(proc_alive(after["claude_pid"], after["claude_ident"]))

    def test_resume_after_stop_adds_a_turn(self):
        self.start("laptop", "w8", "MARK-1")
        self.wait_results("laptop", "w8", 1)
        sid = read_json(os.path.join(self.local, "runs", "w8", "meta.json"))["session_id"]
        self.assertTrue(sid)
        self.ok("stop", "laptop", "w8", "--grace", "10")
        r = self.ok("resume", "laptop", "w8", self.prompt("MARK-RESUMED"), "--json")
        res = json.loads(r.stdout)
        self.assertEqual(res["gen"], 2)
        self.assertEqual(res["session_id"], sid)
        self.assertIn("MARK-RESUMED", self.wait_results("laptop", "w8", 2)[1])
        self.ok("stop", "laptop", "w8", "--grace", "10")

    def test_start_wires_account_cwd_and_claude_args(self):
        cwd = os.path.join(self.tmp, "elsewhere")
        os.makedirs(cwd)
        self.ok("start", "laptop", "wc", self.prompt("MARK-1"), "--account", "acct-x", "--cwd", cwd,
                "--init-timeout", "20", "--", "--model", "sonnet",
                env={"CLAUDE_CODE_SESSION_ID": "lead-session-should-not-leak"})
        self.wait_results("laptop", "wc", 1)
        with open(os.path.join(self.local, "runs", "wc", "out.jsonl")) as f:
            init = [json.loads(l) for l in f if '"init"' in l][0]
        self.assertEqual(init["account"], "acct-x")
        self.assertEqual(os.path.realpath(init["cwd"]), os.path.realpath(cwd))
        self.assertEqual(init["argv"][-2:], ["--model", "sonnet"])
        for flag in ("-p", "--input-format", "stream-json", "--replay-user-messages"):
            self.assertIn(flag, init["argv"])
        self.assertIsNone(init["leaked_session_env"])

    def test_start_uses_default_account_from_config(self):
        self.start("laptop", "wdef", "MARK-1")
        self.wait_results("laptop", "wdef", 1)
        with open(os.path.join(self.local, "runs", "wdef", "out.jsonl")) as f:
            init = [json.loads(l) for l in f if '"init"' in l][0]
        self.assertEqual(init["account"], "work")  # launched through `cswap run work --`

    def test_start_without_account_runs_plain_claude(self):
        """No --account and no default_account: plain `claude`, no cswap."""
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump({"self": ["laptop"]}, f)
        self.start("laptop", "wp", "MARK-PLAIN", env={"CXD_CSWAP": "/nonexistent/cswap"})
        self.assertIn("MARK-PLAIN", self.wait_results("laptop", "wp", 1)[0])
        with open(os.path.join(self.local, "runs", "wp", "out.jsonl")) as f:
            init = [json.loads(l) for l in f if '"init"' in l][0]
        self.assertIsNone(init["account"])  # STUB_ACCOUNT is only set by the fake cswap
        self.assertIn("--input-format", init["argv"])
        self.assertIsNone(read_json(os.path.join(self.local, "runs", "wp", "meta.json"))["account"])
        r = self.ok("list", "laptop")
        self.assertIn("acct=-", r.stdout)
        # Asking for an account without cswap installed is refused up front.
        r = self.cxd("start", "laptop", "wq", self.prompt("MARK-1"), "--account", "work",
                     env={"CXD_CSWAP": "/nonexistent/cswap"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("cswap not found", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.local, "runs", "wq")))

    def test_default_launch_has_no_permission_bypass(self):
        """GUARD bypass-on-by-default. Mutation: --dangerously-skip-permissions is
        back in the default launch command."""
        r = self.ok("start", "laptop", "wn", self.prompt("MARK-1"), "--init-timeout", "20",
                    "--", "--permission-mode", "acceptEdits")
        self.wait_results("laptop", "wn", 1)
        argv = self.inits("laptop", "wn")[0]["argv"]
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[-2:], ["--permission-mode", "acceptEdits"])  # args after -- still pass through
        self.assertIs(read_json(os.path.join(self.local, "runs", "wn", "meta.json"))["bypass_opt_in"], False)
        self.assertIs(self.pids("laptop", "wn")["skip_permissions"], False)  # what the gen really ran with
        # One warning on stderr that names both opt-ins; stdout stays the start line.
        self.assertEqual(r.stderr.count("cxd: warning:"), 1, r.stderr)
        self.assertIn("--skip-permissions", r.stderr)
        self.assertIn('"skip_permissions": true', r.stderr)
        self.assertNotIn("warning", r.stdout)

    def test_permission_bypass_is_opt_in_by_flag_or_host_config(self):
        # 1. `--skip-permissions` on the command line.
        r = self.ok("start", "laptop", "wf", self.prompt("MARK-1"), "--skip-permissions", "--init-timeout", "20",
                    "--", "--model", "sonnet")
        self.wait_results("laptop", "wf", 1)
        argv = self.inits("laptop", "wf")[0]["argv"]
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[-2:], ["--model", "sonnet"])
        self.assertNotIn("warning", r.stderr)
        # A resumed run keeps the choice it was started with.
        self.ok("stop", "laptop", "wf", "--grace", "10")
        self.ok("resume", "laptop", "wf", self.prompt("MARK-2"), "--init-timeout", "20")
        self.wait_results("laptop", "wf", 2)
        self.assertIn("--dangerously-skip-permissions", self.inits("laptop", "wf")[1]["argv"])
        # 2. "skip_permissions": true in the config of the host that runs the worker.
        with open(os.path.join(self.remote, "config.json"), "w") as f:
            json.dump({"default_account": "work", "skip_permissions": True}, f)
        r = self.ok("start", "buildbox", "wk", self.prompt("MARK-1"), "--init-timeout", "20")
        self.wait_results("buildbox", "wk", 1)
        self.assertIn("--dangerously-skip-permissions", self.inits("buildbox", "wk")[0]["argv"])
        self.assertNotIn("warning", r.stderr)
        # It applies to that host only: the laptop's config has no key.
        r = self.ok("start", "laptop", "wl", self.prompt("MARK-1"), "--init-timeout", "20")
        self.wait_results("laptop", "wl", 1)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("laptop", "wl")[0]["argv"])
        self.assertIn("cxd: warning:", r.stderr)
        # 3. Only a JSON true opts in.
        with open(os.path.join(self.remote, "config.json"), "w") as f:
            json.dump({"default_account": "work", "skip_permissions": "false"}, f)
        r = self.ok("start", "buildbox", "ws", self.prompt("MARK-1"), "--init-timeout", "20")
        self.wait_results("buildbox", "ws", 1)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("buildbox", "ws")[0]["argv"])
        self.assertIn("cxd: warning:", r.stderr)

    def test_resume_opts_in_to_bypass_only_when_asked(self):
        """GUARD resume-bypass. Mutation: resume always adds
        --dangerously-skip-permissions -> a resume without an opt-in bypasses."""
        self.start("laptop", "wr1", "MARK-1")
        self.wait_results("laptop", "wr1", 1)
        meta_p = os.path.join(self.local, "runs", "wr1", "meta.json")
        # A run as cxd 0.1.0 wrote it: "skip_permissions" there mixed the run's
        # flag with the host setting, so it is not trusted as an opt-in.
        meta = read_json(meta_p)
        del meta["bypass_opt_in"]
        meta["skip_permissions"] = True
        with open(meta_p, "w") as f:
            json.dump(meta, f)
        self.ok("stop", "laptop", "wr1", "--grace", "10")
        # 1. No opt-in: no bypass, and one warning that names resume's own opt-in.
        r = self.ok("resume", "laptop", "wr1", self.prompt("MARK-2"), "--init-timeout", "20")
        self.wait_results("laptop", "wr1", 2)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("laptop", "wr1")[1]["argv"])
        self.assertEqual(r.stderr.count("cxd: warning:"), 1, r.stderr)
        self.assertIn("cxd resume laptop wr1 <prompt> --skip-permissions", r.stderr)
        self.assertNotIn("cxd start", r.stderr)
        self.ok("stop", "laptop", "wr1", "--grace", "10")
        # 2. --skip-permissions on resume opts in, and the run keeps it.
        r = self.ok("resume", "laptop", "wr1", self.prompt("MARK-3"), "--skip-permissions", "--init-timeout", "20")
        self.wait_results("laptop", "wr1", 3)
        self.assertIn("--dangerously-skip-permissions", self.inits("laptop", "wr1")[2]["argv"])
        self.assertNotIn("warning", r.stderr)
        self.assertIs(read_json(meta_p)["bypass_opt_in"], True)
        self.assertIs(self.pids("laptop", "wr1")["skip_permissions"], True)
        self.ok("stop", "laptop", "wr1", "--grace", "10")
        self.ok("resume", "laptop", "wr1", self.prompt("MARK-4"), "--init-timeout", "20")
        self.wait_results("laptop", "wr1", 4)
        self.assertIn("--dangerously-skip-permissions", self.inits("laptop", "wr1")[3]["argv"])
        self.ok("stop", "laptop", "wr1", "--grace", "10")
        # 3. --no-skip-permissions withdraws the run's opt-in.
        r = self.ok("resume", "laptop", "wr1", self.prompt("MARK-5"), "--no-skip-permissions", "--init-timeout", "20")
        self.wait_results("laptop", "wr1", 5)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("laptop", "wr1")[4]["argv"])
        self.assertIs(read_json(meta_p)["bypass_opt_in"], False)
        self.assertIn("cxd: warning:", r.stderr)
        self.ok("stop", "laptop", "wr1", "--grace", "10")
        r = self.cxd("resume", "laptop", "wr1", self.prompt("x"), "--skip-permissions", "--no-skip-permissions")
        self.assertEqual(r.returncode, 2, r.stderr)

    def remote_bypass(self, value):
        with open(os.path.join(self.remote, "config.json"), "w") as f:
            json.dump({"default_account": "work", "skip_permissions": value}, f)

    def test_resume_honours_the_host_bypass_setting(self):
        """GUARD bypass-json-true. Mutation: any truthy host setting opts in ->
        "skip_permissions": "false" bypasses."""
        self.start("buildbox", "wr2", "MARK-1")
        self.wait_results("buildbox", "wr2", 1)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("buildbox", "wr2")[0]["argv"])
        steps = [("false", False), (True, True), (False, False)]  # only a JSON true; the host's current setting
        for n, (value, bypass) in enumerate(steps, start=2):
            self.ok("stop", "buildbox", "wr2", "--grace", "10")
            self.remote_bypass(value)
            r = self.ok("resume", "buildbox", "wr2", self.prompt("MARK-%d" % n), "--init-timeout", "20")
            self.wait_results("buildbox", "wr2", n)
            argv = self.inits("buildbox", "wr2")[n - 1]["argv"]
            self.assertEqual("--dangerously-skip-permissions" in argv, bypass, (value, argv))
            self.assertEqual("cxd: warning:" in r.stderr, not bypass, (value, r.stderr))
        self.ok("stop", "buildbox", "wr2", "--grace", "10")
        # Withdrawing the run's opt-in cannot undo the host's: say so (exit 5).
        self.remote_bypass(True)
        r = self.cxd("resume", "buildbox", "wr2", self.prompt("MARK-5"), "--no-skip-permissions", "--init-timeout", "20")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("still bypasses permission prompts", r.stderr)
        self.wait_results("buildbox", "wr2", 5)
        self.assertIn("--dangerously-skip-permissions", self.inits("buildbox", "wr2")[4]["argv"])
        self.ok("stop", "buildbox", "wr2", "--grace", "10")

    def test_a_withdrawal_is_kept_even_if_the_resume_fails(self):
        """GUARD withdraw-now. Mutation: a withdrawal is saved only after a good
        launch -> a failed `resume --no-skip-permissions` leaves the opt-in, and
        the next plain resume bypasses again."""
        self.ok("start", "laptop", "wr6", self.prompt("MARK-1"), "--skip-permissions", "--init-timeout", "20")
        self.wait_results("laptop", "wr6", 1)
        self.ok("stop", "laptop", "wr6", "--grace", "10")
        r = self.cxd("resume", "laptop", "wr6", self.prompt("MARK-2"), "--no-skip-permissions",
                     "--init-timeout", "10", env={"STUB_FAIL_START": "1"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        meta = read_json(os.path.join(self.local, "runs", "wr6", "meta.json"))
        self.assertIs(meta["bypass_opt_in"], False)
        self.assertNotIn("skip_permissions", meta)  # the legacy key is gone too
        self.ok("resume", "laptop", "wr6", self.prompt("MARK-3"), "--init-timeout", "20")
        self.wait_results("laptop", "wr6", 2)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("laptop", "wr6")[-1]["argv"])
        self.ok("stop", "laptop", "wr6", "--grace", "10")

    def test_host_bypass_at_start_is_not_kept_by_the_run(self):
        """GUARD host-not-kept. Mutation: start saves the host's setting as the
        run's own opt-in -> the bypass outlives the host setting."""
        self.remote_bypass(True)
        self.start("buildbox", "wr5", "MARK-1")
        self.wait_results("buildbox", "wr5", 1)
        self.assertIn("--dangerously-skip-permissions", self.inits("buildbox", "wr5")[0]["argv"])
        self.assertIs(read_json(os.path.join(self.remote, "runs", "wr5", "meta.json"))["bypass_opt_in"], False)
        self.ok("stop", "buildbox", "wr5", "--grace", "10")
        self.remote_bypass(False)
        self.ok("resume", "buildbox", "wr5", self.prompt("MARK-2"), "--init-timeout", "20")
        self.wait_results("buildbox", "wr5", 2)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("buildbox", "wr5")[1]["argv"])
        self.ok("stop", "buildbox", "wr5", "--grace", "10")

    def test_failed_resume_does_not_keep_the_opt_in(self):
        self.start("laptop", "wr3", "MARK-1")
        self.wait_results("laptop", "wr3", 1)
        self.ok("stop", "laptop", "wr3", "--grace", "10")
        r = self.cxd("resume", "laptop", "wr3", self.prompt("MARK-2"), "--skip-permissions",
                     "--init-timeout", "10", env={"STUB_FAIL_START": "1"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIsNot(read_json(os.path.join(self.local, "runs", "wr3", "meta.json")).get("bypass_opt_in"), True)
        self.ok("resume", "laptop", "wr3", self.prompt("MARK-3"), "--init-timeout", "20")
        self.wait_results("laptop", "wr3", 2)
        self.assertNotIn("--dangerously-skip-permissions", self.inits("laptop", "wr3")[-1]["argv"])
        self.ok("stop", "laptop", "wr3", "--grace", "10")

    def test_opt_in_a_host_ignores_is_an_error(self):
        """GUARD bypass-not-applied. Mutation: the client does not check that a
        requested --skip-permissions was applied -> exit 0, silently unbypassed."""
        shim = os.path.join(self.tmp, "old-cxd")
        with open(shim, "w") as f:
            f.write("import json, subprocess, sys\n"
                    "req = json.loads(sys.stdin.read())\n"
                    "req['args'].pop('skip_permissions', None)\n"
                    "sys.exit(subprocess.run([sys.executable, %r, '_rpc'], input=json.dumps(req),\n"
                    "                        universal_newlines=True).returncode)\n" % CXD)
        with open(os.path.join(self.local, "config.json")) as f:
            cfg = json.load(f)
        cfg["hosts"]["oldbox"] = {"transport": "ssh", "python": sys.executable, "cxd": shim}
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump(cfg, f)
        for verb in ("start", "resume"):
            if verb == "resume":
                self.ok("stop", "oldbox", "wo", "--grace", "10")
            r = self.cxd(verb, "oldbox", "wo", self.prompt("MARK-1"), "--skip-permissions", "--init-timeout", "20")
            self.assertEqual(r.returncode, 5, r.stdout + r.stderr)  # 5: running, but without the bypass
            self.assertIn("did not apply --skip-permissions", r.stderr)
            self.assertIn("is running WITHOUT", r.stderr)
            self.assertIn("wo", r.stdout)  # the start/resume line is still printed
        self.ok("stop", "oldbox", "wo", "--grace", "10")

    def test_start_reports_a_process_that_dies_at_once(self):
        r = self.cxd("start", "laptop", "wd", self.prompt("MARK-1"), "--init-timeout", "10",
                     env={"STUB_FAIL_START": "1"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("exited during start", r.stderr)
        self.assertIn("unknown option", r.stderr)

    def test_start_refuses_existing_name(self):
        self.start("laptop", "w9", "MARK-1")
        r = self.cxd("start", "laptop", "w9", self.prompt("again"))
        self.assertEqual(r.returncode, 3)
        self.assertIn("already exists", r.stderr)


class TestInit(Base):
    def test_init_writes_config_and_links_cli(self):
        home = os.path.join(self.tmp, "fresh", ".cxd")
        env = {"CXD_HOME": home}
        self.ok("init", "--self", "laptop", "--peer", "buildbox", "--account", "work",
                "--account", "personal", "--account", "work", "--default-account", "work", env=env)
        cfg = read_json(os.path.join(home, "config.json"))
        self.assertEqual(cfg["self"], ["laptop"])
        self.assertEqual(cfg["hosts"], {"buildbox": {"transport": "ssh"}})
        self.assertEqual(cfg["accounts"], ["work", "personal"])
        self.assertEqual(cfg["default_account"], "work")
        link = os.path.join(home, "bin", "cxd")
        self.assertEqual(os.path.realpath(link), os.path.realpath(CXD))
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
        # A bare re-run keeps the config and refreshes the link.
        self.ok("init", env=env)
        self.assertEqual(read_json(os.path.join(home, "config.json")), cfg)
        self.assertEqual(os.path.realpath(link), os.path.realpath(CXD))
        r = subprocess.run([sys.executable, link, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertIn("cxd ", r.stdout)


class TestWatch(Base):
    def test_watch_local_turns_pr_exit(self):
        self.start("laptop", "wa", "MARK-1")
        self.wait_results("laptop", "wa", 1)
        p, log = self.watch_bg("laptop", "wa")
        self.wait_event(log, lambda e: e["ev"] == "status")
        self.ok("send", "laptop", "wa", "PR MARK-2")
        self.wait_event(log, lambda e: e["ev"] == "turn" and "MARK-2" in e["result"])
        self.wait_event(log, lambda e: e["ev"] == "pr" and e["url"].endswith("/pull/7"))
        self.ok("stop", "laptop", "wa", "--grace", "10")
        e = self.wait_event(log, lambda e: e["ev"] == "exit")
        self.assertEqual(e["rc"], 0)
        p.wait(10)
        self.assertEqual(p.returncode, 0)

    def test_watch_survives_ssh_failures_without_reporting_exit(self):
        """GUARD watch-transport. Mutation: watch reports 'exit' when the ssh
        channel fails -> an exit is reported for a run that is alive."""
        self.start("buildbox", "wr", "MARK-1")
        self.wait_results("buildbox", "wr", 1)
        fail = os.path.join(self.tmp, "ssh-fail")
        with open(fail, "w") as f:
            f.write("2")
        p, log = self.watch_bg("buildbox", "wr", env={"FAKE_SSH_FAIL_FILE": fail, "FAKE_SSH_DROP_AFTER": "4"})
        self.wait_event(log, lambda e: e["ev"] == "status", timeout=40)
        transport = [e for e in self.read_events(log) if e["ev"] == "transport"]
        self.assertEqual(len(transport), 2, self.log_text(log))
        self.assertFalse([e for e in self.read_events(log) if e["ev"] == "exit"], self.log_text(log))
        # Each stream is cut 4 s after it connects (rc 255). The turn below
        # takes 8 s from after this stream connected, so it ends after the cut
        # and must arrive on a reconnected stream, exactly once.
        self.ok("send", "buildbox", "wr", "SLEEP 8 MARK-ACROSSDROP")
        self.wait_event(log, lambda e: e["ev"] == "turn" and "MARK-ACROSSDROP" in e["result"], timeout=60)
        evs, text = self.read_events(log), self.log_text(log)
        kinds = [e["ev"] for e in evs]
        turn_at = [i for i, e in enumerate(evs) if e["ev"] == "turn" and "MARK-ACROSSDROP" in e["result"]]
        self.assertEqual(len(turn_at), 1, text)
        self.assertGreaterEqual(kinds[:turn_at[0]].count("transport"), 3,
                                "the turn was not delivered across a stream cut:\n" + text)
        self.assertFalse([e for e in evs if e["ev"] == "exit"], text)
        self.assertIsNone(p.poll(), "watch stopped while the run is alive:\n" + text)
        pids = self.pids("buildbox", "wr")
        self.assertTrue(proc_alive(pids["claude_pid"], pids["claude_ident"]))
        self.ok("stop", "buildbox", "wr", "--grace", "10")
        self.wait_event(log, lambda e: e["ev"] == "exit", timeout=40)
        p.wait(20)
        self.assertEqual(p.returncode, 0)
        evs, text = self.read_events(log), self.log_text(log)
        # Reconnecting from the cursor never repeated the turn.
        self.assertEqual(len([e for e in evs if e["ev"] == "turn" and "MARK-ACROSSDROP" in e["result"]]), 1, text)
        # Exactly one exit, and it is the host's: rc, gen and turns of the run, no reason.
        exits = [e for e in evs if e["ev"] == "exit"]
        self.assertEqual(len(exits), 1, text)
        self.assertNotIn("reason", exits[0])
        self.assertEqual((exits[0]["rc"], exits[0]["gen"], exits[0]["turns"]), (0, 1, 2), exits[0])


class TestExitRecord(Base):
    """watch reports a run's exit with the rc its supervisor records. claude
    is reaped (and so already gone) a moment before exit.json is written."""

    def fake_run(self, name, supervisor_alive, gen=1, pids=True, claude=None):
        """A run directory; claude is a live process standing in for claude, or
        None for one that is gone."""
        from cxd.common import atomic_write_json, proc_identity
        d = os.path.join(self.local, "runs", name)
        os.makedirs(d, mode=0o700)
        gone = subprocess.Popen(["true"])
        gone.wait()
        rec = {"gen": gen, "claude_pid": gone.pid, "claude_ident": "never:" + name,
               "keeper_pid": gone.pid, "keeper_ident": "never:" + name}
        if claude is not None:
            rec.update(claude_pid=claude.pid, claude_ident=proc_identity(claude.pid))
        if supervisor_alive:
            sup = subprocess.Popen(["sleep", "60"])
            self.bg.append(sup)
            rec.update(supervisor_pid=sup.pid, supervisor_ident=proc_identity(sup.pid))
        atomic_write_json(os.path.join(d, "meta.json"), {"name": name, "gen": gen})
        if pids:
            atomic_write_json(os.path.join(d, "pids.json"), rec)
        with open(os.path.join(d, "out.jsonl"), "w") as f:
            f.write(json.dumps({"type": "cxd", "event": "gen-start", "gen": gen}) + "\n")
        return d

    def home_env(self):
        return mock.patch.dict(os.environ, {"CXD_HOME": self.local})

    def first_exit(self, name, timeout=30):
        from cxd import state
        t0 = time.time()
        with self.home_env():
            for msg in state.stream({"target": name, "interval": 0.1, "heartbeat": 3600, "keepalive": 0.2}):
                if msg.get("type") == "event" and msg["event"]["ev"] == "exit":
                    return msg["event"], time.time() - t0
                if time.time() - t0 > timeout:
                    self.fail("no exit event within %ss" % timeout)

    def test_a_break_between_two_events_of_one_batch_loses_neither(self):
        """GUARD event-cursor. Mutation: every message of a batch carries the
        cursor from after the batch -> a client that drops after `turn`
        resumes past `exit` and waits forever."""
        from cxd import state
        from cxd.common import atomic_write_json
        d = self.fake_run("wb", supervisor_alive=False)
        with open(os.path.join(d, "out.jsonl"), "a") as f:
            f.write(json.dumps({"type": "result", "subtype": "success", "result": "MARK-LAST",
                                "total_cost_usd": 0.01}) + "\n")
        atomic_write_json(os.path.join(d, "exit.json"), {"gen": 1, "rc": 0})
        with self.home_env():
            msgs, t0 = [], time.time()
            for m in state.stream({"target": "wb", "interval": 0.1, "heartbeat": 3600, "keepalive": 0.2,
                                   "from_start": True}):
                msgs.append(m)
                if m.get("type") == "end" or time.time() - t0 > 10:
                    break
            turn = [m for m in msgs if m.get("type") == "event" and m["event"]["ev"] == "turn"][0]
            # The client dropped right after `turn`: resume from its cursor.
            t0 = time.time()
            resumed = []
            for m in state.stream({"target": "wb", "interval": 0.1, "heartbeat": 3600, "keepalive": 0.2,
                                   "cursor": json.loads(json.dumps(turn["cursor"]))}):
                resumed.append(m)
                if m.get("type") == "end" or time.time() - t0 > 5:
                    break
        evs = [m["event"]["ev"] for m in resumed if m.get("type") == "event"]
        self.assertEqual(evs, ["exit"], resumed)

    SID = "77777777-1111-2222-3333-444444444444"

    def run_with_lines(self, name, lines, claude=None):
        d = self.fake_run(name, supervisor_alive=False, claude=claude)
        self.add_lines(d, lines)
        if claude is None:
            self.exited(d)
        return d

    def add_lines(self, d, lines):
        with open(os.path.join(d, "out.jsonl"), "a") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def exited(self, d, rc=0):
        from cxd.common import atomic_write_json
        atomic_write_json(os.path.join(d, "exit.json"), {"gen": 1, "rc": rc})

    def live_claude(self):
        p = subprocess.Popen(["sleep", "60"])
        self.bg.append(p)
        return p

    @staticmethod
    def text(t):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}

    def assert_resumes_exactly(self, target, events, rest_after_poll=()):
        """From every event's cursor, a poll gives exactly the events after it:
        in the same order within one poll; across polls a resumed poll may give
        the output's events before the reports', but none lost, none twice, and
        the exit last."""
        from cxd import state
        strip = lambda evs: [{k: v for k, v in e.items() if k != "_cursor"} for e in evs]
        for i, e in enumerate(events):
            rest, _ = state.poll(target, json.loads(json.dumps(e["_cursor"])))
            want = strip(list(events[i + 1:]) + list(rest_after_poll))
            got = strip(rest)
            if rest_after_poll:
                key = lambda x: json.dumps(x, sort_keys=True)
                self.assertEqual(sorted(got, key=key), sorted(want, key=key), "resumed after event %d (%s)" % (i, e))
                self.assertEqual(got[-1:], want[-1:])
            else:
                self.assertEqual(got, want, "resumed after event %d (%s)" % (i, e))

    def notify(self, text):
        os.makedirs(os.path.join(self.local, "outbox"), mode=0o700, exist_ok=True)
        with open(os.path.join(self.local, "outbox", self.SID + ".jsonl"), "a") as f:
            f.write(json.dumps({"kind": "notify", "session_id": self.SID, "text": text}) + "\n")

    def test_resuming_after_any_event_delivers_exactly_the_rest(self):
        """GUARD event-line-skip. Mutation: every event of a line carries the
        cursor from after the line -> a client that drops after the first PR of
        a line (or after the PR of a final result) never gets the rest of it."""
        from cxd import state
        text = self.text
        self.run_with_lines("wl", [
            {"type": "system", "subtype": "init", "session_id": self.SID, "model": "m"},
            text("see https://github.com/o/r/pull/1 and https://github.com/o/r/pull/2"),
            text("MARK-NO-EVENT"),
            {"type": "result", "subtype": "success", "result": "opened https://github.com/o/r/pull/3",
             "total_cost_usd": 0.01, "session_id": self.SID},
            {"type": "result", "subtype": "success", "result": "MARK-LAST", "total_cost_usd": 0.03,
             "session_id": self.SID},
        ])
        self.notify("MARK-NOTIFY")
        with self.home_env():
            events, _ = state.poll("wl", {})
            self.assertEqual([e["ev"] for e in events],
                             ["init", "pr", "pr", "pr", "turn", "turn", "outbox", "exit"], events)
            self.assertEqual((events[-1]["turns"], events[-1]["cost_usd"]), (2, 0.03))
            self.assert_resumes_exactly("wl", events)

    def test_resuming_across_polls_delivers_exactly_the_rest(self):
        """GUARD event-cursor-deep. Mutation: an event's cursor is a shallow copy
        -> the outbox offsets in it move on with the rest of the poll, and a
        client that drops after an output event loses the reports after it."""
        from cxd import state
        claude = self.live_claude()
        d = self.run_with_lines("wx", [{"type": "system", "subtype": "init", "session_id": self.SID}], claude=claude)
        self.notify("MARK-1")
        with self.home_env():
            first, cur = state.poll("wx", {})
            self.assertEqual([e["ev"] for e in first], ["init", "outbox"])
            self.add_lines(d, [self.text("https://github.com/o/r/pull/7 https://github.com/o/r/pull/8"),
                               {"type": "result", "subtype": "success", "result": "MARK-T1"}])
            self.notify("MARK-2")
            claude.kill()
            claude.wait()
            self.exited(d)
            second, _ = state.poll("wx", cur)
            self.assertEqual([e["ev"] for e in second], ["pr", "pr", "turn", "outbox", "exit"], second)
            self.assert_resumes_exactly("wx", second)
            self.assert_resumes_exactly("wx", first, rest_after_poll=second)

    def test_a_live_claude_clears_an_earlier_wait_from_every_event(self):
        """GUARD exit-wait-alive. Mutation: a live claude leaves an earlier exit
        wait in the cursor -> a client that resumes from such an event later
        cuts the wait for the exit code short (rc=None)."""
        from cxd import state
        d = self.run_with_lines("wa", [], claude=self.live_claude())
        self.add_lines(d, [{"type": "result", "subtype": "success", "result": "x"}])
        stale = {"exit_wait": {"gen": 1, "t": time.time() - 9}}  # a check that once failed
        with self.home_env():
            events, cur = state.poll("wa", stale)
        self.assertEqual([e["ev"] for e in events], ["turn"])
        self.assertNotIn("exit_wait", events[0]["_cursor"])
        self.assertNotIn("exit_wait", cur)

    def test_the_scan_before_the_exit_reads_to_the_end(self):
        """GUARD exit-drain. Mutation: the scan before the exit reads one chunk
        like any other -> after a long run the exit comes with too few turns and
        the rest of its events are never sent."""
        from cxd import state
        self.run_with_lines("wd", [{"type": "result", "subtype": "success", "result": "MARK-%d" % i,
                                    "total_cost_usd": 0.01 * (i + 1)} for i in range(10)])
        with self.home_env(), mock.patch.object(state, "SCAN_CHUNK", 300):
            events, _ = state.poll("wd", {})
        self.assertEqual([e["ev"] for e in events], ["turn"] * 10 + ["exit"])
        self.assertEqual((events[-1]["turns"], events[-1]["cost_usd"]), (10, 0.1))

    def test_a_long_run_is_read_a_chunk_per_poll_while_it_lives(self):
        from cxd import state
        self.run_with_lines("wk", [{"type": "result", "subtype": "success", "result": "MARK-%d" % i}
                                   for i in range(10)], claude=self.live_claude())
        with self.home_env(), mock.patch.object(state, "SCAN_CHUNK", 300):
            events, cur = state.poll("wk", {})
            self.assertLess(len(events), 10)
            while len(events) < 10:
                more, cur = state.poll("wk", cur)
                self.assertTrue(more)
                events += more
        self.assertEqual([e["turn"] for e in events], list(range(1, 11)))

    def test_a_notify_written_as_the_run_exits_comes_before_the_exit(self):
        """The exit is decided before the scan, so a report written while the
        run exits is read before the exit (the watch ends there)."""
        from cxd import state
        self.run_with_lines("wn", [{"type": "system", "subtype": "init", "session_id": self.SID}])
        real = state._exit_record

        def late_notify(d, gen):  # the worker reports while the run exits
            self.notify("MARK-LATE")
            return real(d, gen)
        with self.home_env(), mock.patch.object(state, "_exit_record", late_notify):
            events, _ = state.poll("wn", {})
        self.assertEqual([e["ev"] for e in events], ["init", "outbox", "exit"], events)
        self.assertEqual(events[1]["text"], "MARK-LATE")

    def test_events_of_the_poll_that_starts_the_exit_wait_carry_it(self):
        """A break right after such an event does not start the wait again."""
        # (the wait is decided before the scan)
        from cxd import state
        d = self.fake_run("ww", supervisor_alive=True)
        with open(os.path.join(d, "out.jsonl"), "a") as f:
            f.write(json.dumps({"type": "result", "subtype": "success", "result": "x"}) + "\n")
        with self.home_env():
            events, cur = state.poll("ww", {})
        self.assertEqual([e["ev"] for e in events], ["turn"])
        self.assertEqual(events[0]["_cursor"]["exit_wait"], cur["exit_wait"])

    def test_a_poll_never_changes_the_cursor_it_was_given(self):
        from cxd import state
        self.run_with_lines("wc", [{"type": "system", "subtype": "init", "session_id": self.SID}])
        self.notify("MARK-1")
        with self.home_env():
            _, cur = state.poll("wc", {"exit_gen": 1})
            before = json.loads(json.dumps(cur))
            self.notify("MARK-2")
            state.poll("wc", cur)
        self.assertEqual(cur, before)

    def test_watch_waits_for_the_recorded_exit_code(self):
        """GUARD exit-rc. Mutation: the exit is reported as soon as claude is
        gone, before the supervisor has recorded the rc -> rc=None."""
        from cxd.common import atomic_write_json
        d = self.fake_run("wz", supervisor_alive=True)

        def record():
            time.sleep(1.5)
            atomic_write_json(os.path.join(d, "exit.json"), {"gen": 1, "rc": 7})

        th = threading.Thread(target=record)
        th.start()
        try:
            ev, _ = self.first_exit("wz")
        finally:
            th.join()
        self.assertEqual((ev["rc"], ev["gen"]), (7, 1), ev)

    def test_watch_does_not_wait_when_the_supervisor_is_gone(self):
        self.fake_run("wy", supervisor_alive=False)
        ev, took = self.first_exit("wy")
        self.assertIsNone(ev["rc"])
        self.assertLess(took, 3)

    def test_wait_is_capped_when_the_supervisor_hangs(self):
        """GUARD exit-rc-cap. Mutation: no cap -> a supervisor that never writes
        exit.json keeps watch from ever reporting the exit."""
        from cxd import state
        self.fake_run("wh", supervisor_alive=True)
        with mock.patch.object(state, "EXIT_RECORD_WAIT", 1.0):
            ev, took = self.first_exit("wh", timeout=15)
        self.assertIsNone(ev["rc"])
        self.assertGreaterEqual(took, 1.0)

    def test_a_wait_left_from_an_earlier_gen_does_not_cut_this_one(self):
        from cxd import state
        from cxd.common import atomic_write_json
        d = self.fake_run("wg", supervisor_alive=True, gen=2)
        # A cursor (round-tripped as JSON, as over ssh) still timing gen 1's exit.
        for stale in ({"gen": 1, "t": time.time() - 30}, time.time() - 30):
            cur = json.loads(json.dumps({"exit_gen": 1, "exit_wait": stale}))
            with self.home_env():
                events, cur = state.poll("wg", cur)
            self.assertEqual([e for e in events if e["ev"] == "exit"], [], (stale, events))
        atomic_write_json(os.path.join(d, "exit.json"), {"gen": 2, "rc": 5})
        with self.home_env():
            events, cur = state.poll("wg", json.loads(json.dumps(cur)))
        self.assertEqual([(e["rc"], e["gen"]) for e in events if e["ev"] == "exit"], [(5, 2)])

    def test_exit_record_renamed_by_resume_is_read(self):
        from cxd.common import atomic_write_json
        d = self.fake_run("wv", supervisor_alive=False)
        # resume moves exit.json aside as exit.<gen>.json before the next gen starts.
        atomic_write_json(os.path.join(d, "exit.1.json"), {"gen": 1, "rc": 3})
        ev, _ = self.first_exit("wv")
        self.assertEqual((ev["rc"], ev["gen"]), (3, 1), ev)

    def test_watch_ends_when_the_run_failed_before_it_had_pids(self):
        from cxd.common import atomic_write_json
        d = self.fake_run("wf", supervisor_alive=False, pids=False)
        atomic_write_json(os.path.join(d, "exit.json"),
                          {"gen": 1, "rc": None, "error": "keeper failed to open the FIFO"})
        ev, took = self.first_exit("wf", timeout=10)
        self.assertEqual((ev["rc"], ev["gen"], ev.get("error")), (None, 1, "keeper failed to open the FIFO"))


class TestSupervisorFailure(Base):
    def test_unexpected_failure_before_claude_records_an_exit_and_ends_the_keeper(self):
        """GUARD supervise-record. Mutation: the supervisor's guard is gone -> an
        unexpected error leaves no exit record (watch waits forever) and an
        orphaned keeper holding the FIFO."""
        import errno as _errno
        from cxd import supervise
        from cxd.common import atomic_write_json
        d = os.path.join(self.local, "runs", "wx")
        os.makedirs(d, mode=0o700)
        atomic_write_json(os.path.join(d, "meta.json"), {"name": "wx", "gen": 1})
        env = {"CXD_HOME": self.local, "CXD_CLAUDE": self.env["CXD_CLAUDE"]}
        with mock.patch.dict(os.environ, env), mock.patch.object(supervise.signal, "signal"), \
                mock.patch.object(supervise, "append_jsonl", side_effect=OSError(_errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                supervise.supervise_main(d, 1)
        # If the guard failed, end the keeper it leaked (our own child) after the test.
        leaked = (read_json(os.path.join(d, "keeper.ready"), {}) or {}).get("pid")

        def end_leaked_keeper():
            try:
                os.kill(leaked, signal.SIGTERM)
                os.waitpid(leaked, 0)
            except (OSError, ChildProcessError, TypeError):
                pass
        self.addCleanup(end_leaked_keeper)
        ex = read_json(os.path.join(d, "exit.json"))
        self.assertEqual((ex["gen"], ex["rc"]), (1, None), ex)
        self.assertIn("disk full", ex["error"])
        # No writer is left on the FIFO: a non-blocking read sees EOF, not EAGAIN.
        fd = os.open(os.path.join(d, "in.fifo"), os.O_RDONLY | os.O_NONBLOCK)
        try:
            deadline = time.time() + 5
            while True:
                try:
                    self.assertEqual(os.read(fd, 1), b"")
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        self.fail("the keeper still holds the FIFO open")
                    time.sleep(0.1)
        finally:
            os.close(fd)


class TestRemote(Base):
    def test_remote_start_send_stop_resume_list(self):
        self.start("buildbox", "rr", "MARK-R1")
        self.assertIn("MARK-R1", self.wait_results("buildbox", "rr", 1)[0])
        self.assertFalse(os.path.isdir(os.path.join(self.local, "runs")), "run state leaked to the local host")
        self.ok("send", "buildbox", "rr", "MARK-R2")
        self.assertIn("MARK-R2", self.wait_results("buildbox", "rr", 2)[1])
        r = self.ok("list", "--json")
        hosts = {h["host"]: h for h in json.loads(r.stdout)}
        self.assertEqual(hosts["buildbox"]["runs"][0]["turns"], 2)
        self.assertEqual(hosts["laptop"]["runs"], [])
        self.ok("stop", "buildbox", "rr", "--grace", "10")
        self.ok("resume", "buildbox", "rr", self.prompt("MARK-R3"))
        self.assertIn("MARK-R3", self.wait_results("buildbox", "rr", 3)[2])
        self.ok("stop", "buildbox", "rr", "--grace", "10")

    def test_transport_failure_is_exit_4_not_a_refusal(self):
        fail = os.path.join(self.tmp, "ssh-fail")
        with open(fail, "w") as f:
            f.write("1")
        r = self.cxd("list", "buildbox", env={"FAKE_SSH_FAIL_FILE": fail})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("UNREACHABLE", r.stdout)


FAKE_CLAUDE = r'''# A process whose argv[0] is "claude" and that runs plugin hooks as children,
# the way Claude Code does. Commands on stdin: "<Event>" runs that hook; the
# hook's stdout goes to $OUTDIR/<n>.out and "<n> <rc>" is printed.
# bash, not Python: a macOS framework Python re-execs itself and loses argv[0].
sid="$1"
n=0
while IFS= read -r ev; do
  [ -n "$ev" ] || continue
  n=$((n + 1))
  printf '{"session_id": "%s", "hook_event_name": "%s", "cwd": "%s", "source": "startup", "last_assistant_message": "worker report MARK-STOP"}' "$sid" "$ev" "$PWD" \
    | /bin/sh -c '"$HOOK" "$1"' sh "$ev" > "$OUTDIR/$n.out"
  echo "$n $?"
done
'''


class TestHooks(Base):
    def fake_session(self, sid):
        bindir = os.path.join(self.tmp, "fakebin")
        os.makedirs(bindir, exist_ok=True)
        script = os.path.join(bindir, "fake-claude.sh")
        with open(script, "w") as f:
            f.write(FAKE_CLAUDE)
        outdir = os.path.join(self.tmp, "hook-out-" + sid[:8])
        os.makedirs(outdir)
        env = dict(self.env, HOOK=HOOK, OUTDIR=outdir)
        # argv[0] "claude", like the real native binary.
        p = subprocess.Popen(["claude", script, sid], executable="/bin/bash", stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, env=env, universal_newlines=True, bufsize=1)
        p.outdir = outdir
        self.bg.append(p)
        return p

    def hook(self, p, event):
        p.stdin.write(event + "\n")
        p.stdin.flush()
        n, rc = p.stdout.readline().split()
        with open(os.path.join(p.outdir, n + ".out")) as f:
            out = f.read()
        return {"event": event, "rc": int(rc), "out": out}

    def test_hook_inbox_delivery_exactly_once_and_stop_report(self):
        sid = "11111111-2222-3333-4444-555555555555"
        p = self.fake_session(sid)
        start = self.hook(p, "SessionStart")
        self.assertEqual(start["rc"], 0)
        ctx = json.loads(start["out"])["hookSpecificOutput"]["additionalContext"]
        tag = read_json(os.path.join(self.local, "sessions", sid + ".json"))["tag"]
        self.assertGreaterEqual(len(tag), 16)
        self.assertIn("[cxd:%s]" % tag, ctx)  # the session learns how ITS lead messages are marked
        rec = read_json(os.path.join(self.local, "sessions", sid + ".json"))
        self.assertEqual(rec["pid"], p.pid, "SessionStart registered the wrong pid")
        w, log = self.watch_bg("laptop", sid)
        self.wait_event(log, lambda e: e["ev"] == "status" and e["alive"] is True)
        r = self.ok("send", "laptop", sid[:8], "please include MARK-HOOK")
        self.assertIn("via inbox", r.stdout)
        first = self.hook(p, "PostToolUse")
        ctx = json.loads(first["out"])["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertTrue(ctx["additionalContext"].startswith("Message from the lead session "))
        self.assertIn("MARK-HOOK", ctx["additionalContext"])
        self.assertIn("[cxd:%s]" % tag, ctx["additionalContext"])
        self.assertEqual(self.hook(p, "PostToolUse")["out"], "")  # delivered once
        self.ok("send", "laptop", sid, "late MARK-AT-STOP")
        stop = self.hook(p, "Stop")
        self.assertIn("MARK-AT-STOP", json.loads(stop["out"])["hookSpecificOutput"]["additionalContext"])
        self.wait_event(log, lambda e: e["ev"] == "outbox" and e["kind"] == "delivered")
        self.wait_event(log, lambda e: e["ev"] == "outbox" and e["kind"] == "stop" and "MARK-STOP" in e["text"])
        self.hook(p, "SessionEnd")
        self.wait_event(log, lambda e: e["ev"] == "exit")
        r = self.cxd("send", "laptop", sid, "too late")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not running", r.stderr)

    def session_as(self, sid, argv0, script_path, extra=()):
        """A fake session whose process looks like a given Claude Code launch."""
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w") as f:
            f.write(FAKE_CLAUDE)
        outdir = os.path.join(self.tmp, "hook-out-" + sid[:8])
        os.makedirs(outdir)
        env = dict(self.env, HOOK=HOOK, OUTDIR=outdir)
        p = subprocess.Popen([argv0, script_path, sid] + list(extra), executable="/bin/bash",
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                             universal_newlines=True, bufsize=1)
        p.outdir = outdir
        self.bg.append(p)
        self.hook(p, "SessionStart")
        return p, read_json(os.path.join(self.local, "sessions", sid + ".json"))

    def test_every_claude_code_launch_is_recognised(self):
        """GUARD npm-claude. Mutation: a JS runtime running Claude Code is not
        recognised -> such a session gets no pid of its own (or an outer
        session's), so sends to it are refused or go by the wrong liveness."""
        npm = os.path.join(self.tmp, "lib", "node_modules", "@anthropic-ai", "claude-code")
        cases = [
            ("dddd0001-2222-3333-4444-555555555555", "node", os.path.join(self.tmp, "prefix", "bin", "claude")),
            ("dddd0002-2222-3333-4444-555555555555", "node", os.path.join(npm, "cli.js")),
            ("dddd0003-2222-3333-4444-555555555555", os.path.join(npm, "bin", "claude.exe"),
             os.path.join(self.tmp, "fake-claude-exe.sh")),
        ]
        for sid, argv0, script in cases:
            p, rec = self.session_as(sid, argv0, script)
            self.assertEqual(rec["pid"], p.pid, (argv0, script))
            self.ok("send", "laptop", sid, "MARK-LAUNCH")

    def test_an_ancestor_that_only_mentions_claude_code_is_not_it(self):
        sid = "dddd0004-2222-3333-4444-555555555555"
        p, rec = self.session_as(sid, "sh", os.path.join(self.tmp, "wrapper.sh"),
                                 extra=["/x/node_modules/@anthropic-ai/claude-code/cli.js"])
        self.assertNotEqual(rec["pid"], p.pid)

    def test_send_refused_when_session_process_is_gone(self):
        sid = "22222222-2222-3333-4444-555555555555"
        p = self.fake_session(sid)
        self.hook(p, "SessionStart")
        p.kill()
        p.wait()
        r = self.cxd("send", "laptop", sid, "hello")
        self.assertEqual(r.returncode, 3)
        self.assertIn("process gone", r.stderr)

    def test_concurrent_hooks_claim_each_message_once(self):
        sid = "33333333-2222-3333-4444-555555555555"
        inbox = os.path.join(self.local, "inbox", sid)
        os.makedirs(inbox, mode=0o700)
        for i in range(30):
            with open(os.path.join(inbox, "%019d-%06d.json" % (i, i)), "w") as f:
                json.dump({"id": str(i), "ts": "t", "sender": "s", "text": "MSG-%d-END" % i}, f)
        outs = []

        def drain():
            r = subprocess.run([HOOK, "PostToolUse"], input=json.dumps({"session_id": sid}),
                               stdout=subprocess.PIPE, universal_newlines=True, env=self.env)
            outs.append(r.stdout)

        ts = [threading.Thread(target=drain) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        while os.listdir(inbox):
            drain()
        seen = []
        for o in outs:
            if o.strip():
                ctx = json.loads(o)["hookSpecificOutput"]["additionalContext"]
                seen += [int(x.split("-")[1]) for x in ctx.split() if x.startswith("MSG-")]
        self.assertEqual(sorted(seen), list(range(30)))

    def queue(self, sid, i, text):
        inbox = os.path.join(self.local, "inbox", sid)
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        mid = "%019d-%06d" % (i, i)
        with open(os.path.join(inbox, mid + ".json"), "w") as f:
            json.dump({"id": mid, "ts": "t", "sender": "s", "text": text}, f)
        return mid

    def run_hook(self, sid, event="PostToolUse", broken_stdout=False):
        payload = json.dumps({"session_id": sid}).encode()
        if not broken_stdout:
            r = subprocess.run([HOOK, event], input=payload, stdout=subprocess.PIPE, env=self.env)
            return r.stdout.decode()
        # The reader is gone before the hook writes: its output can never be read.
        p = subprocess.Popen([HOOK, event], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
        p.stdout.close()
        p.stdin.write(payload)
        p.stdin.close()
        p.wait(10)
        return None

    def receipts(self, sid):
        try:
            with open(os.path.join(self.local, "outbox", sid + ".jsonl")) as f:
                return [json.loads(l) for l in f if '"delivered"' in l]
        except OSError:
            return []

    def test_message_a_hook_could_not_hand_over_is_delivered_by_the_next(self):
        """GUARD ack-after-write. Mutation: a message counts as delivered when it
        is claimed, before the hook's output is written -> a hook that cannot
        hand it over loses it."""
        sid = "66666666-2222-3333-4444-555555555555"
        mid = self.queue(sid, 1, "MARK-AT-LEAST-ONCE")
        self.assertIsNone(self.run_hook(sid, broken_stdout=True))
        self.assertEqual(self.receipts(sid), [])  # not acknowledged
        out = self.run_hook(sid)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("MARK-AT-LEAST-ONCE", ctx)
        self.assertIn(mid, ctx)  # the id travels with the message (duplicates are recognisable)
        self.assertEqual([r["id"] for r in self.receipts(sid)], [mid])
        self.assertEqual(self.run_hook(sid), "")  # and then it is done
        self.assertEqual(os.listdir(os.path.join(self.local, "delivered", sid)), [mid + ".json"])

    def test_claim_of_a_dead_hook_is_requeued(self):
        """GUARD redeliver. Mutation: claims are never recovered -> a message
        claimed by a hook that died is never delivered."""
        sid = "77777777-2222-3333-4444-555555555555"
        mid = self.queue(sid, 2, "MARK-CLAIMED-BY-DEAD")
        gone = subprocess.Popen(["true"])
        gone.wait()
        claimed = os.path.join(self.local, "claimed", sid)
        os.makedirs(claimed, mode=0o700)
        os.rename(os.path.join(self.local, "inbox", sid, mid + ".json"),
                  os.path.join(claimed, "%s.claim-%d-%s.json" % (mid, gone.pid, "0" * 12)))
        os.rmdir(os.path.join(self.local, "inbox", sid))  # only the claim is left
        out = self.run_hook(sid, "UserPromptSubmit")
        self.assertIn("MARK-CLAIMED-BY-DEAD", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(os.listdir(claimed), [])

    def test_claim_of_a_live_hook_is_left_alone(self):
        sid = "88888888-2222-3333-4444-555555555555"
        mid = self.queue(sid, 3, "MARK-CLAIMED-BY-LIVE")
        sys.path.insert(0, os.path.join(REPO, "lib"))
        from cxd.hook import claim_token
        live = subprocess.Popen(["sleep", "30"])
        self.bg.append(live)
        claimed = os.path.join(self.local, "claimed", sid)
        os.makedirs(claimed, mode=0o700)
        os.rename(os.path.join(self.local, "inbox", sid, mid + ".json"),
                  os.path.join(claimed, "%s.claim-%s.json" % (mid, claim_token(live.pid))))
        self.assertEqual(self.run_hook(sid), "")  # another hook holds it
        self.assertEqual(len(os.listdir(claimed)), 1)

    def test_claim_of_a_recycled_pid_is_requeued(self):
        """GUARD claim-identity. Mutation: only the pid is compared -> a live
        process that reuses a dead claimer's pid keeps the claim forever."""
        sid = "abcdabcd-2222-3333-4444-555555555555"
        mid = self.queue(sid, 4, "MARK-RECYCLED")
        live = subprocess.Popen(["sleep", "30"])
        self.bg.append(live)
        claimed = os.path.join(self.local, "claimed", sid)
        os.makedirs(claimed, mode=0o700)
        os.rename(os.path.join(self.local, "inbox", sid, mid + ".json"),
                  os.path.join(claimed, "%s.claim-%d-%s.json" % (mid, live.pid, "f" * 12)))
        out = self.run_hook(sid)
        self.assertIn("MARK-RECYCLED", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_a_malformed_inbox_file_is_set_aside(self):
        sid = "bcdebcde-2222-3333-4444-555555555555"
        inbox = os.path.join(self.local, "inbox", sid)
        os.makedirs(inbox, mode=0o700)
        for name, body in (("0000000000000000001-000001.json", "[1]"), ("0000000000000000002-000002.json", "")):
            with open(os.path.join(inbox, name), "w") as f:
                f.write(body)
        self.queue(sid, 3, "MARK-AFTER-BAD")
        out = self.run_hook(sid)
        self.assertIn("MARK-AFTER-BAD", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.local, "bad", sid))),
                         ["0000000000000000001-000001.json", "0000000000000000002-000002.json"])
        self.assertEqual(self.run_hook(sid), "")

    def test_a_long_message_arrives_whole_in_parts(self):
        sid = "cdefcdef-2222-3333-4444-555555555555"
        p = self.fake_session(sid)
        self.hook(p, "SessionStart")
        words = ["w%05d" % i for i in range(3000)]  # about 21,000 characters
        text = " ".join(words)
        r = self.ok("send", "laptop", sid, text, "--json")
        res = json.loads(r.stdout)
        self.assertGreater(res["parts"], 1)
        self.assertEqual(len(res["ids"]), res["parts"])
        r = self.cxd("send", "laptop", sid, "-", stdin="y" * 1000001)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("too long", r.stderr)
        got = []
        for _ in range(10):
            res = self.hook(p, "PostToolUse")
            if not res["out"]:
                break
            ctx = json.loads(res["out"])["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("truncated", ctx)
            self.assertLessEqual(len(ctx), 10000)
            got.append(ctx)
        joined = " ".join(" ".join(c.split()) for c in got)
        self.assertEqual([w for w in joined.split() if w.startswith("w") and w[1:].isdigit()], words)
        self.assertIn("[part 1/", got[0])

    def test_split_text_keeps_every_character(self):
        from cxd.state import _split_text
        cases = ["a" * 6000 + " ", ("line of text\n" * 2000), ("    indented\n" * 1500) + "end",
                 "x" * 13000, "", "short"]
        for text in cases:
            parts = _split_text(text, 6000)
            self.assertEqual("".join(parts), text)
            self.assertTrue(all(0 < len(p) <= 6000 for p in parts) or text == "", [len(p) for p in parts])

    def test_a_hook_without_its_own_identity_claims_nothing(self):
        """GUARD claim-needs-identity. Mutation: a hook claims without being able
        to read its own identity -> its claim could never be recovered."""
        from cxd import hook
        sid = "efefefef-2222-3333-4444-555555555555"
        self.queue(sid, 5, "MARK-NO-IDENTITY")
        with mock.patch.dict(os.environ, {"CXD_HOME": self.local}), \
                mock.patch.object(hook, "proc_identity", return_value=None):
            self.assertEqual(hook.claim_inbox(sid), [])
        self.assertEqual(len(os.listdir(os.path.join(self.local, "inbox", sid))), 1)
        self.assertFalse(os.path.exists(os.path.join(self.local, "claimed", sid)) and
                         os.listdir(os.path.join(self.local, "claimed", sid)))

    def session_start(self, sid, source="startup", env=None):
        e = dict(self.env)
        e.update(env or {})
        r = subprocess.run([HOOK, "SessionStart"], input=json.dumps({"session_id": sid, "source": source}).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""

    def test_the_announcement_names_a_secret_tag_and_messages_carry_it(self):
        """GUARD announce-tag. Mutation: messages go out without the session's
        tag -> the session cannot tell them from the same words in a file or
        web page, which the announcement tells it to ignore."""
        sid = "bebebebe-2222-3333-4444-555555555555"
        ctx = self.session_start(sid)
        tag = read_json(os.path.join(self.local, "sessions", sid + ".json"))["tag"]
        self.assertIn("[cxd:%s]" % tag, ctx)
        self.assertIn("lacks that exact mark", ctx)  # untagged copies are not from the user
        self.assertIn("Keep the mark to yourself", ctx)
        self.assertIn("redelivery", ctx)  # at-least-once: a repeated id is not a new instruction
        self.queue(sid, 1, "MARK-TAGGED")
        out = json.loads(self.run_hook(sid))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[cxd:%s]" % tag, out)
        # A resume keeps the tag and does not announce again; /clear and a compaction do.
        self.assertEqual(self.session_start(sid, source="resume"), "")
        self.assertEqual(read_json(os.path.join(self.local, "sessions", sid + ".json"))["tag"], tag)
        for source in ("clear", "compact"):
            self.assertIn("[cxd:%s]" % tag, self.session_start(sid, source=source))
        # A resumed session without a tag (a fork, an older record) gets one, and is told.
        rec_p = os.path.join(self.local, "sessions", sid + ".json")
        rec = read_json(rec_p)
        del rec["tag"]
        with open(rec_p, "w") as f:
            json.dump(rec, f)
        ctx = self.session_start(sid, source="resume")
        new_tag = read_json(rec_p)["tag"]
        self.assertNotEqual(new_tag, tag)
        self.assertIn("[cxd:%s]" % new_tag, ctx)
        # Another session gets another tag.
        self.session_start("bebebebe-9999")
        self.assertNotEqual(read_json(os.path.join(self.local, "sessions", "bebebebe-9999.json"))["tag"], tag)

    def test_no_announcement_in_headless_runs_or_when_turned_off(self):
        self.assertEqual(self.session_start("bebebebe-3333", env={"CXD_RUN": "w1"}), "")
        with open(os.path.join(self.local, "config.json")) as f:
            cfg = json.load(f)
        cfg["announce"] = False
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.session_start("bebebebe-4444"), "")
        self.assertFalse(os.path.exists(os.path.join(self.local, "delivered", "bebebebe-4444")))

    def test_announcement_and_mail_fit_the_context_cap(self):
        sid = "bebebebe-5555-3333-4444-555555555555"
        for i in range(40):
            self.queue(sid, i, "x" * 400)
        ctx = self.session_start(sid)
        # The announcement is budgeted: announcement + mail stay within the message budget.
        self.assertLessEqual(len(ctx), 9000)
        self.assertNotIn("truncated", ctx)
        self.assertIn("x" * 400, ctx)

    def test_an_announcement_alone_creates_no_delivered_dir(self):
        self.session_start("bebebebe-6666")
        self.assertFalse(os.path.exists(os.path.join(self.local, "delivered", "bebebebe-6666")))

    def test_a_message_a_session_sent_itself_is_not_tagged(self):
        """GUARD self-not-tagged. Mutation: a session's message to itself gets its
        tag -> a session (or anything it runs) can mint instructions to itself."""
        sid = "bebebebe-7777-3333-4444-555555555555"
        p = self.fake_session(sid)
        self.hook(p, "SessionStart")
        tag = read_json(os.path.join(self.local, "sessions", sid + ".json"))["tag"]
        self.ok("send", "laptop", sid, "MARK-SELF", env={"CLAUDE_CODE_SESSION_ID": sid})
        ctx = json.loads(self.hook(p, "PostToolUse")["out"])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("MARK-SELF", ctx)
        self.assertNotIn("[cxd:%s]" % tag, ctx)
        self.assertIn("sent by this session itself", ctx)

    def test_session_start_is_silent_where_cxd_is_not_set_up(self):
        os.unlink(os.path.join(self.local, "config.json"))
        r = subprocess.run([HOOK, "SessionStart"], input=json.dumps({"session_id": "bebebebe-1111"}).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_hook_never_breaks_a_session(self):
        cases = [b"", b"not json", b"[]", json.dumps({"session_id": "../../etc"}).encode()]
        for inp in cases:
            for ev in ("SessionStart", "PostToolUse", "Stop", "SessionEnd", "UserPromptSubmit"):
                r = subprocess.run([HOOK, ev], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self.env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""), (ev, inp))
        # An unwritable home must not break it either.
        bad = dict(self.env, CXD_HOME="/nonexistent/forbidden/.cxd")
        r = subprocess.run([HOOK, "Stop"], input=json.dumps({"session_id": "abcd1234"}).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=bad)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_hook_is_fast(self):
        payload = json.dumps({"session_id": "44444444-aaaa", "last_assistant_message": "x"}).encode()
        for ev, limit in (("PostToolUse", 100), ("Stop", 100)):
            ts = []
            for _ in range(7):
                t0 = time.time()
                subprocess.run([HOOK, ev], input=payload, stdout=subprocess.PIPE, env=self.env)
                ts.append((time.time() - t0) * 1000)
            med = sorted(ts)[3]
            print("hook %s median %.1f ms" % (ev, med), file=sys.stderr)
            self.assertLess(med, limit)
        # A hook that delivers (claim, write, ack) is held to the same limit.
        sid = "44444444-bbbb"
        ts = []
        for i in range(7):
            self.queue(sid, i, "MARK-FAST-%d" % i)
            t0 = time.time()
            out = self.run_hook(sid)
            ts.append((time.time() - t0) * 1000)
            self.assertIn("MARK-FAST-%d" % i, out)
        med = sorted(ts)[3]
        print("hook PostToolUse delivering median %.1f ms" % med, file=sys.stderr)
        self.assertLess(med, 100)

    def test_notify_lands_in_outbox(self):
        r = self.ok("notify", "built it MARK-N", env={"CLAUDE_CODE_SESSION_ID": "55555555-aaaa"})
        self.assertIn("55555555-aaaa", r.stdout)
        with open(os.path.join(self.local, "outbox", "55555555-aaaa.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual((rec["kind"], rec["text"]), ("notify", "built it MARK-N"))
        r = self.cxd("notify", "x")
        self.assertEqual(r.returncode, 2)


PIN = os.path.join(REPO, "scripts", "check-release-pin.py")


def git_env():
    """Fixture git runs must never touch the repo under test: drop GIT_DIR and
    friends (set inside git hooks and worktrees) and ignore global config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


class TestReleasePin(unittest.TestCase):
    """The marketplace installs the latest tagged release, pinned by commit."""

    URL = "https://example.invalid/cxd.git"

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="cxd-pin-")
        os.makedirs(os.path.join(self.repo, ".claude-plugin"))
        self.manifest("9.9.9")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "release 9.9.9")
        self.git("tag", "v9.9.9")
        self.sha = self.git("rev-parse", "HEAD")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", self.repo]
                           + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, check=True, env=git_env())
        return r.stdout.strip()

    def commit(self, msg, version=None):
        if version:
            self.manifest(version)
        with open(os.path.join(self.repo, "work.txt"), "a") as f:
            f.write(msg + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def manifest(self, version, repository="https://example.invalid/cxd"):
        with open(os.path.join(self.repo, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "cswap-cross-device", "version": version, "repository": repository}, f)

    def write_market(self, market):
        with open(os.path.join(self.repo, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump(market, f)

    def market(self, **src):
        source = dict({"source": "url", "url": self.URL, "ref": "v9.9.9", "sha": self.sha}, **src)
        entry = {"name": "cswap-cross-device", "source": source}
        self.write_market({"name": "m", "owner": {"name": "o"}, "plugins": [entry]})
        return entry

    def pin(self):
        r = subprocess.run([sys.executable, PIN, "--repo", self.repo], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, env=git_env())
        return r.returncode, r.stdout

    def assert_fails(self, msg):
        rc, out = self.pin()
        self.assertEqual(rc, 1, out)
        self.assertIn(msg, out)
        self.assertNotIn("Traceback", out)

    def test_consistent_pin_passes(self):
        self.market()
        self.assertEqual(self.pin(), (0, "release pin: ok\n"))
        # An annotated tag works the same way.
        sha = self.commit("release 9.9.10", version="9.9.10")
        self.git("tag", "-a", "-m", "v9.9.10", "v9.9.10")
        self.market(ref="v9.9.10", sha=sha)
        self.assertEqual(self.pin(), (0, "release pin: ok\n"))

    def test_the_check_ignores_a_git_dir_it_inherits(self):
        """GUARD release-pin-git-env. Mutation: the check passes GIT_DIR on to
        git -> run from a git hook it checks the wrong repository."""
        self.market()
        decoy = tempfile.mkdtemp(prefix="cxd-decoy-")
        self.addCleanup(shutil.rmtree, decoy, True)
        subprocess.run(["git", "init", "-q", decoy], check=True, env=git_env())
        env = dict(git_env(), GIT_DIR=os.path.join(decoy, ".git"))
        r = subprocess.run([sys.executable, PIN, "--repo", self.repo], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, "release pin: ok\n"))

    def test_sha_of_another_commit_fails(self):
        """GUARD release-pin-sha. Mutation: the sha is not compared with the
        tag's commit -> a pin to an unreleased commit passes."""
        self.market(sha=self.commit("unreleased work on main"))
        self.assert_fails("is not the commit of v9.9.9")

    def test_an_older_release_fails(self):
        """GUARD release-pin-latest. Mutation: the newest-tag check is off ->
        a pin left on an older release passes."""
        self.commit("release 9.10.0", version="9.10.0")
        self.git("tag", "v9.10.0")
        self.market()
        self.assert_fails("pins v9.9.9, but the newest release tag is v9.10.0")

    def test_newest_means_newest_merged(self):
        # A tag on a commit that is not merged (here) does not count as newer.
        self.git("checkout", "-q", "-b", "side")
        self.commit("side release", version="9.10.0")
        self.git("tag", "v9.10.0")
        self.git("checkout", "-q", "-")
        self.market()
        self.assertEqual(self.pin(), (0, "release pin: ok\n"))

    def test_a_tag_on_an_unmerged_commit_fails(self):
        """GUARD release-pin-merged. Mutation: the ancestry check is off -> a
        release tagged on a side branch passes."""
        self.git("checkout", "-q", "-b", "side")
        side = self.commit("side release", version="9.9.10")
        self.git("tag", "v9.9.10")
        self.git("checkout", "-q", "-")
        self.market(ref="v9.9.10", sha=side)
        self.assert_fails("is not in the history of HEAD")

    def test_tag_whose_manifest_has_another_version_fails(self):
        """GUARD release-pin-version. Mutation: the version is not compared ->
        a tag whose plugin.json says another version passes."""
        sha = self.commit("bump without a matching tag", version="9.9.10")
        self.git("tag", "v9.9.11")
        self.market(ref="v9.9.11", sha=sha)
        self.assert_fails("says version '9.9.10', the tag says 9.9.11")

    def test_url_must_be_https(self):
        """GUARD release-pin-https. Mutation: any url scheme passes."""
        self.market(url="git@example.invalid:cxd.git")
        self.assert_fails("url must be an https:// git URL")

    def test_other_broken_pins_fail(self):
        cases = [
            (dict(ref="main"), "ref must be a release tag"),
            (dict(ref="v9.9.8"), "tag v9.9.8 does not exist"),
            (dict(ref="v9.9.\u00b9"), "ref must be a release tag"),
            (dict(sha="abc123"), "sha must be the 40-character commit"),
            (dict(url="https://example.invalid/other.git"), "is not the plugin's repository"),
        ]
        for src, msg in cases:
            self.market(**src)
            self.assert_fails(msg)
        # A branch named like a release is not a release tag.
        self.git("branch", "v9.9.7")
        self.market(ref="v9.9.7")
        self.assert_fails("tag v9.9.7 does not exist")
        # A relative-path source installs main, not a release.
        self.write_market({"name": "m", "owner": {"name": "o"},
                           "plugins": [{"name": "cswap-cross-device", "source": "./"}]})
        self.assert_fails("source must be a git url source")
        # A version on the entry competes with plugin.json.
        entry = self.market()
        entry["version"] = "9.9.9"
        self.write_market({"name": "m", "owner": {"name": "o"}, "plugins": [entry]})
        self.assert_fails("drop 'version' from the entry")
        # Malformed files give a message, not a traceback.
        for bad in ([], {"plugins": []}, {"plugins": ["x"]}):
            self.write_market(bad)
            rc, out = self.pin()
            self.assertEqual(rc, 1, out)
            self.assertNotIn("Traceback", out)


class TestVersion(unittest.TestCase):
    def test_cli_version_matches_the_plugin_manifest(self):
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json")) as f:
            version = json.load(f)["version"]
        r = subprocess.run([sys.executable, CXD, "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "cxd " + version))
class TestMutationTable(unittest.TestCase):
    MUTATE = os.path.join(REPO, "tests", "mutate.py")

    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cxd_mutate", self.MUTATE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_table_is_well_formed(self):
        m = self.load()
        self.assertEqual(m.check_table(m.MUTATIONS), [])

    def test_a_folded_entry_is_refused(self):
        """GUARD table-fold. Mutation: the source check is off -> an entry that a
        bad merge folded into the next goes unnoticed (and its guard unchecked)."""
        m = self.load()
        with open(self.MUTATE) as f:
            src = f.read()
        # Fold the first entry into the second, as a sloppy conflict resolution does.
        i = src.index('        "name": "', src.index("MUTATIONS = ["))
        j = src.index("\n    },\n    {\n", i)
        folded = src[:j] + "\n" + src[j + len("\n    },\n    {\n"):]
        problems = m.check_source(folded)
        self.assertTrue(problems and "repeats key" in problems[0], problems)
        with mock.patch("builtins.open", mock.mock_open(read_data=folded)):
            self.assertTrue(any("repeats key" in p for p in m.check_table([])))

    def test_other_bad_entries_are_refused(self):
        m = self.load()
        first = dict(m.MUTATIONS[0])
        cases = [
            ([first, dict(first)], "the name must be a unique string"),
            ([dict(first, edits=[("lib/cxd/cli.py", "x", "x")])], "changes something"),
            ([dict(first, edits=[("lib/cxd/cli.py", "no such anchor", "y")])], "anchor found 0 times"),
            ([dict(first, edits=[("../outside.py", "a", "b")])], "inside the repository"),
            ([dict(first, tests=1)], "tests must be a non-empty list"),
            ([dict(first, expect="maybe")], "expect must be red or green"),
            ([{k: v for k, v in first.items() if k != "what"}], "missing ['what']"),
            ([dict(first, **{"1": 1})], "unexpected ['1']"),
        ]
        with mock.patch.object(m, "guard_names_in_tests", return_value=set()):
            self.assertEqual(m.check_table([first]), [])  # the baseline is clean
            for table, msg in cases:
                problems = m.check_table(table)
                self.assertTrue(any(msg in p for p in problems), (msg, problems))
        # Every GUARD named in a test docstring needs an entry.
        self.assertTrue(any("no mutation" in p for p in m.check_table([first])))

    def test_an_unknown_name_fails(self):
        r = subprocess.run([sys.executable, self.MUTATE, "no-such-guard"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("unknown mutation", r.stdout)
class TestPrune(Base):
    DAY = 86400
    fake_session = TestHooks.fake_session
    hook = TestHooks.hook

    def session_files(self, sid):
        h = self.local
        return [p for p in (os.path.join(h, "sessions", sid + ".json"), os.path.join(h, "outbox", sid + ".jsonl"),
                            os.path.join(h, "inbox", sid), os.path.join(h, "claimed", sid),
                            os.path.join(h, "delivered", sid)) if os.path.exists(p)]

    def fake_record(self, sid, ended=True, age_days=10, pid=None, ident=None, run=None):
        """A session record with mail and reports, last active age_days ago."""
        from cxd.common import atomic_write_json
        for d in ("sessions", "outbox", "inbox/" + sid, "delivered/" + sid):
            os.makedirs(os.path.join(self.local, d), mode=0o700, exist_ok=True)
        atomic_write_json(os.path.join(self.local, "sessions", sid + ".json"), {
            "session_id": sid, "pid": pid, "pid_ident": ident, "cxd_run": run,
            "ended": "2026-01-01T00:00:00+0000" if ended else None})
        with open(os.path.join(self.local, "outbox", sid + ".jsonl"), "w") as f:
            f.write('{"kind": "stop", "text": "x"}\n')
        for sub in ("inbox", "delivered"):
            with open(os.path.join(self.local, sub, sid, "0001-000001.json"), "w") as f:
                f.write('{"id": "0001-000001", "text": "x"}')
        old = time.time() - age_days * self.DAY
        for p in self.session_files(sid):
            for root, dirs, files in os.walk(p) if os.path.isdir(p) else [(None, [], [])]:
                for n in files:
                    os.utime(os.path.join(root, n), (old, old))
            os.utime(p, (old, old))

    def age_tree(self, top, when):
        for root, dirs, files in os.walk(top):
            for n in files + dirs:
                os.utime(os.path.join(root, n), (when, when), follow_symlinks=False)
        os.utime(top, (when, when))

    def prune(self, *args):
        r = self.ok("prune", "local", "--json", *args)
        return json.loads(r.stdout)

    def test_ended_sessions_are_pruned_after_the_configured_age(self):
        """GUARD prune-age. Mutation: age is ignored -> a session that ended
        yesterday is pruned with the old ones."""
        self.fake_record("aaaa0000-old-ended", ended=True, age_days=10)
        self.fake_record("aaaa0001-new-ended", ended=True, age_days=1)
        self.fake_record("aaaa0002-old-crashed", ended=False, age_days=10, pid=999999999, ident="never")
        res = self.prune()
        self.assertEqual(sorted(res["sessions"]), ["aaaa0000-old-ended", "aaaa0002-old-crashed"])
        self.assertEqual(self.session_files("aaaa0000-old-ended"), [])
        self.assertEqual(self.session_files("aaaa0002-old-crashed"), [])
        self.assertEqual(len(self.session_files("aaaa0001-new-ended")), 4)
        # The age is configurable, in config.json or per call.
        res = self.prune("--older-than", "0.5")
        self.assertEqual(res["sessions"], ["aaaa0001-new-ended"])

    def test_live_sessions_are_never_pruned(self):
        """GUARD prune-live. Mutation: liveness is not checked -> a live session
        that has been quiet for long is pruned."""
        sid = "99999999-2222-3333-4444-555555555555"
        p = self.fake_session(sid)
        self.hook(p, "SessionStart")
        rec = read_json(os.path.join(self.local, "sessions", sid + ".json"))
        self.fake_record(sid, ended=False, age_days=30, pid=rec["pid"], ident=rec["pid_ident"])
        res = self.prune("--older-than", "1")
        self.assertEqual(res["sessions"], [])
        self.assertEqual(len(self.session_files(sid)), 4)
        self.ok("send", "laptop", sid, "still reachable MARK-LIVE")

    def test_a_runs_session_goes_with_the_run(self):
        self.start("laptop", "wp1", "MARK-1")
        self.wait_results("laptop", "wp1", 1)
        self.ok("stop", "laptop", "wp1", "--grace", "10")
        self.fake_record("bbbb0000-run-session", ended=True, age_days=10, run="wp1")
        old = time.time() - 10 * self.DAY
        self.age_tree(os.path.join(self.local, "runs", "wp1"), old)
        res = self.prune()
        self.assertEqual((res["sessions"], res["runs"]), ([], []))  # runs only with --runs
        res = self.prune("--runs", "--dry-run")
        self.assertEqual((res["sessions"], res["runs"], res["dry_run"]), (["bbbb0000-run-session"], ["wp1"], True))
        self.assertTrue(os.path.isdir(os.path.join(self.local, "runs", "wp1")))
        res = self.prune("--runs")
        self.assertEqual((res["sessions"], res["runs"]), (["bbbb0000-run-session"], ["wp1"]))
        self.assertFalse(os.path.exists(os.path.join(self.local, "runs", "wp1")))
        self.assertEqual(self.session_files("bbbb0000-run-session"), [])

    def test_a_running_run_is_never_pruned(self):
        self.start("laptop", "wp2", "MARK-1")
        self.wait_results("laptop", "wp2", 1)
        old = time.time() - 10 * self.DAY
        self.age_tree(os.path.join(self.local, "runs", "wp2"), old)
        res = self.prune("--runs", "--older-than", "1")
        self.assertEqual(res["runs"], [])
        self.ok("send", "laptop", "wp2", "MARK-2")

    def wait_gone(self, sid, timeout=15):
        deadline = time.time() + timeout
        while self.session_files(sid) and time.time() < deadline:
            time.sleep(0.1)
        return self.session_files(sid)

    def test_session_start_prunes_at_most_hourly_in_the_background(self):
        with open(os.path.join(self.local, "config.json")) as f:
            cfg = json.load(f)
        cfg.pop("auto_prune", None)  # the base test config turns it off
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump(cfg, f)
        sid = "cccc0000-2222-3333-4444-555555555555"
        self.fake_record("cccc0001-old-ended", ended=True, age_days=10)
        p = self.fake_session(sid)
        t0 = time.time()
        self.assertEqual(self.hook(p, "SessionStart")["rc"], 0)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.wait_gone("cccc0001-old-ended"), [])  # pruned by a detached process
        self.fake_record("cccc0002-old-ended", ended=True, age_days=10)
        self.hook(p, "SessionStart")  # within the hour: no second pass
        time.sleep(1.5)
        self.assertEqual(len(self.session_files("cccc0002-old-ended")), 4)
        # A stamp dated in the future (the clock stepped back) does not stop pruning.
        future = time.time() + 7200
        os.utime(os.path.join(self.local, "prune.stamp"), (future, future))
        self.hook(p, "SessionStart")
        self.assertEqual(self.wait_gone("cccc0002-old-ended"), [])
        regs = read_json(os.path.join(self.local, "registrations.json"))
        self.assertIn(read_json(os.path.join(self.local, "sessions", sid + ".json"))["account"], regs)

    def test_stray_temp_files_and_odd_meta_are_handled(self):
        os.makedirs(os.path.join(self.local, "sessions"), mode=0o700, exist_ok=True)
        tmp = os.path.join(self.local, "sessions", "x.json.tmp.123.0a1b2c3d")
        keep = os.path.join(self.local, "sessions", "a.tmp.b.json")  # a session id may contain ".tmp."
        for p in (tmp, keep):
            with open(p, "w") as f:
                f.write("{}")
            os.utime(p, (time.time() - 10 * self.DAY,) * 2)
        os.makedirs(os.path.join(self.local, "runs", "odd"), mode=0o700)
        with open(os.path.join(self.local, "runs", "odd", "meta.json"), "w") as f:
            json.dump({"session_id": ["not", "a", "string"]}, f)
        self.prune()
        self.assertFalse(os.path.exists(tmp))
        self.assertTrue(os.path.exists(keep))  # a session record (no pid: unknown, kept), not a temp file

    def test_an_unlistable_runs_dir_aborts_the_pass(self):
        self.fake_record("bcbc0000-run-session", ended=True, age_days=10, run="gone-run")
        with open(os.path.join(self.local, "runs"), "w") as f:  # runs/ is not a directory
            f.write("x")
        r = self.cxd("prune", "local", "--json")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.session_files("bcbc0000-run-session")), 4)

    def test_a_session_whose_process_is_unknown_is_kept_until_it_ends(self):
        """GUARD prune-unknown. Mutation: a session that never recorded its pid
        (or its process identity) counts as dead -> a live session (e.g. an
        npm-installed claude, or a `ps` that timed out) is pruned."""
        self.fake_record("ffff0000-no-pid-open", ended=False, age_days=30, pid=None, ident=None)
        self.fake_record("ffff0002-no-ident-open", ended=False, age_days=30, pid=os.getpid(), ident=None)
        self.fake_record("ffff0001-no-pid-ended", ended=True, age_days=30, pid=None, ident=None)
        res = self.prune()
        self.assertEqual(res["sessions"], ["ffff0001-no-pid-ended"])
        self.assertEqual(len(self.session_files("ffff0000-no-pid-open")), 4)
        self.assertEqual(len(self.session_files("ffff0002-no-ident-open")), 4)

    def test_configured_age_and_invalid_overrides(self):
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump({"self": ["laptop"], "default_account": "work", "prune_after_days": 20}, f)
        self.fake_record("abab0000-15-days", ended=True, age_days=15)
        self.fake_record("abab0001-25-days", ended=True, age_days=25)
        res = self.prune()
        self.assertEqual((res["older_than_days"], res["sessions"]), (20.0, ["abab0001-25-days"]))
        for bad in ("0", "-1", "nan", "inf"):
            r = self.cxd("prune", "local", "--older-than", bad)
            self.assertEqual(r.returncode, 2, (bad, r.stdout, r.stderr))
        self.assertEqual(len(self.session_files("abab0000-15-days")), 4)

    def test_run_sessions_are_protected_by_the_run_itself(self):
        # The worker's session may have no record (plugin not enabled there):
        # meta.json's session_id still ties it to the run.
        self.start("laptop", "wp3", "MARK-1")
        self.wait_results("laptop", "wp3", 1)
        sid = read_json(os.path.join(self.local, "runs", "wp3", "meta.json"))["session_id"]
        self.fake_record(sid, ended=True, age_days=10)
        os.unlink(os.path.join(self.local, "sessions", sid + ".json"))
        self.assertEqual(self.prune()["sessions"], [])
        self.assertTrue(os.path.exists(os.path.join(self.local, "outbox", sid + ".jsonl")))

    def test_a_run_resumed_during_the_pass_is_kept(self):
        """GUARD prune-run-recheck. Mutation: no re-check before removing a run
        -> a run resumed between the scan and the removal is deleted."""
        from cxd import prune as prune_mod
        from cxd.common import load_config
        d = os.path.join(self.local, "runs", "wp4")
        os.makedirs(d, mode=0o700)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"name": "wp4", "gen": 1}, f)
        self.age_tree(d, time.time() - 10 * self.DAY)
        self.fake_record("eeee0000-wp4-session", ended=True, age_days=10, run="wp4")
        with mock.patch.dict(os.environ, {"CXD_HOME": self.local}), \
                mock.patch.object(prune_mod, "_run_alive", side_effect=[False, True]):
            res = prune_mod.prune(load_config(), runs=True)
        self.assertEqual((res["runs"], res["sessions"]), ([], []))
        self.assertTrue(os.path.isdir(d))
        # The run's session data goes only with the run, so it survives too.
        self.assertEqual(len(self.session_files("eeee0000-wp4-session")), 4)

    def test_a_failed_identity_lookup_keeps_a_live_session(self):
        """GUARD prune-tristate. Mutation: a process whose identity cannot be
        read (a `ps` that timed out) counts as dead -> a live session is pruned."""
        from cxd import common, prune as prune_mod
        from cxd.common import load_config, proc_identity
        self.fake_record("f0f0f0f0-live-unreadable", ended=False, age_days=30,
                         pid=os.getpid(), ident=proc_identity(os.getpid()))
        with mock.patch.dict(os.environ, {"CXD_HOME": self.local}), \
                mock.patch.object(common, "_read_identity", return_value=("unknown", None)):
            res = prune_mod.prune(load_config())
        self.assertEqual(res["sessions"], [])
        self.assertEqual(len(self.session_files("f0f0f0f0-live-unreadable")), 4)

    def test_run_liveness_rules(self):
        """GUARD prune-run-unknown. Mutation: a run without pids.json counts as
        over -> a run still starting up is removed."""
        from cxd.common import atomic_write_json
        old = time.time() - 10 * self.DAY

        def run(name, files):
            d = os.path.join(self.local, "runs", name)
            os.makedirs(d, mode=0o700)
            for fname, body in files.items():
                atomic_write_json(os.path.join(d, fname), body)
            self.age_tree(d, old)
        run("r-starting", {"meta.json": {"name": "r-starting"}})  # no pids yet, no exit
        run("r-failed", {"meta.json": {"name": "r-failed"}, "exit.json": {"gen": 1, "rc": None, "error": "x"}})
        run("r-no-ident", {"meta.json": {"name": "r-no-ident"},
                           "pids.json": {"gen": 1, "claude_pid": os.getpid(), "claude_ident": None}})
        res = self.prune("--runs")
        self.assertEqual(res["runs"], ["r-failed"])

    def test_a_session_of_two_runs_stays_while_either_stays(self):
        from cxd.common import atomic_write_json
        old = time.time() - 10 * self.DAY
        sid = "c1c1c1c1-two-runs"
        for name, files in (("ra", {"meta.json": {"name": "ra"}, "exit.json": {"gen": 1, "rc": 0}}),
                            ("rb", {"meta.json": {"name": "rb", "session_id": sid}})):  # rb: unknown, stays
            d = os.path.join(self.local, "runs", name)
            os.makedirs(d, mode=0o700)
            for fname, body in files.items():
                atomic_write_json(os.path.join(d, fname), body)
            self.age_tree(d, old)
        self.fake_record(sid, ended=True, age_days=10, run="ra")
        res = self.prune("--runs")
        self.assertEqual((res["runs"], res["sessions"]), (["ra"], []))
        self.assertEqual(len(self.session_files(sid)), 4)

    def test_earlier_session_ids_of_a_run_are_protected(self):
        from cxd.common import atomic_write_json
        self.start("laptop", "wp5", "MARK-1")
        self.wait_results("laptop", "wp5", 1)
        meta_p = os.path.join(self.local, "runs", "wp5", "meta.json")
        meta = read_json(meta_p)
        self.assertEqual(meta["session_ids"], [meta["session_id"]])
        meta["session_ids"].insert(0, "c2c2c2c2-earlier-gen")
        atomic_write_json(meta_p, meta)
        self.fake_record("c2c2c2c2-earlier-gen", ended=True, age_days=10)  # its record says nothing of the run
        self.assertEqual(self.prune()["sessions"], [])
        self.assertTrue(os.path.exists(os.path.join(self.local, "outbox", "c2c2c2c2-earlier-gen.jsonl")))

    def test_odd_pids_in_records_do_not_stop_the_pass(self):
        from cxd.common import atomic_write_json
        self.fake_record("c3c3c3c3-odd-pid", ended=False, age_days=10)
        atomic_write_json(os.path.join(self.local, "sessions", "c3c3c3c3-odd-pid.json"),
                          {"session_id": "c3c3c3c3-odd-pid", "pid": 1e30, "pid_ident": "x"})
        os.utime(os.path.join(self.local, "sessions", "c3c3c3c3-odd-pid.json"), (time.time() - 10 * self.DAY,) * 2)
        self.fake_record("c3c3c3c4-old-ended", ended=True, age_days=10)
        res = self.prune()
        self.assertIn("c3c3c3c4-old-ended", res["sessions"])
        self.assertNotIn("c3c3c3c3-odd-pid", res["sessions"])  # unknown: kept

    def test_process_identity_does_not_depend_on_the_time_zone(self):
        """GUARD identity-utc. Mutation: the ps-based identity (macOS) is read in
        local time -> after a time-zone change a live process looks dead."""
        from cxd import common
        if not shutil.which("ps"):
            self.skipTest("no ps")
        # POSIX zone strings need no tzdata, so this works on any CI image.
        with mock.patch.object(common.sys, "platform", "darwin"):  # force the ps path
            with mock.patch.dict(os.environ, {"TZ": "AAA-2"}):
                ident = common.proc_identity(os.getpid())
                _, legacy = common._read_identity(os.getpid(), legacy=True)
            self.assertTrue(ident.startswith("psutc:"), ident)
            with mock.patch.dict(os.environ, {"TZ": "BBB+5"}):
                self.assertEqual(common.proc_state(os.getpid(), ident), "alive")
                self.assertTrue(common.proc_alive(os.getpid(), ident))
                # A record from before ("ps:", local time) in another zone: unknown, never dead.
                self.assertEqual(common.proc_state(os.getpid(), legacy), "unknown")
            with mock.patch.dict(os.environ, {"TZ": "AAA-2"}):
                # ...and in its own zone it is still recognised (cxd stop finds pre-upgrade runs).
                self.assertEqual(common.proc_state(os.getpid(), legacy), "alive")

    def test_runs_of_another_host_are_left_alone(self):
        from cxd.common import atomic_write_json
        d = os.path.join(self.local, "runs", "r-elsewhere")
        os.makedirs(d, mode=0o700)
        atomic_write_json(os.path.join(d, "meta.json"), {"name": "r-elsewhere", "host": "some-other-machine"})
        atomic_write_json(os.path.join(d, "exit.json"), {"gen": 1, "rc": 0})
        self.age_tree(d, time.time() - 10 * self.DAY)
        self.assertEqual(self.prune("--runs")["runs"], [])

    def test_never_prints_as_valid_json(self):
        with open(os.path.join(self.local, "config.json"), "w") as f:
            json.dump({"self": ["laptop"], "prune_after_days": 10 ** 400}, f)
        res = self.prune()  # json.loads in prune() would fail on Infinity
        self.assertIsNone(res["older_than_days"])

    def test_records_of_another_host_are_left_alone(self):
        from cxd.common import atomic_write_json
        self.fake_record("c4c4c4c4-other-host", ended=False, age_days=10, pid=999999999, ident="linux:1")
        rec_p = os.path.join(self.local, "sessions", "c4c4c4c4-other-host.json")
        rec = read_json(rec_p)
        rec["host"] = "some-other-machine"
        atomic_write_json(rec_p, rec)
        os.utime(rec_p, (time.time() - 10 * self.DAY,) * 2)
        self.assertEqual(self.prune()["sessions"], [])

    def test_malformed_records_do_not_stop_the_pass(self):
        self.fake_record("acac0000-list-record", ended=True, age_days=10)
        with open(os.path.join(self.local, "sessions", "acac0000-list-record.json"), "w") as f:
            f.write("[1]")
        os.utime(os.path.join(self.local, "sessions", "acac0000-list-record.json"),
                 (time.time() - 10 * self.DAY,) * 2)
        self.fake_record("acac0001-old-ended", ended=True, age_days=10)
        res = self.prune()
        self.assertIn("acac0001-old-ended", res["sessions"])

    def test_a_broken_prune_setting_breaks_nothing(self):
        for bad in ("soon", -3, None, [1], 10 ** 400):
            with open(os.path.join(self.local, "config.json"), "w") as f:
                json.dump({"self": ["laptop"], "default_account": "work", "prune_after_days": bad}, f)
            self.fake_record("dddd0000-new-ended", ended=True, age_days=1)
            res = self.prune()  # falls back to the default (7 days)
            self.assertEqual(res["sessions"], [], bad)
            try:
                os.unlink(os.path.join(self.local, "prune.stamp"))  # let SessionStart prune again
            except OSError:
                pass
            for ev in ("SessionStart", "Stop"):
                r = subprocess.run([HOOK, ev], input=json.dumps({"session_id": "eeee0000-x"}).encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
                self.assertEqual((r.returncode, r.stderr), (0, b""), (bad, ev))


class TestSecretsCheck(unittest.TestCase):
    def test_a_large_file_scans_without_pipe_errors(self):
        """With SIGPIPE ignored (as under some git hook runners), the early exit
        of `grep -q` must not make the scan print write errors."""
        d = tempfile.mkdtemp(prefix="cxd-secrets-")
        self.addCleanup(shutil.rmtree, d, True)
        big = os.path.join(d, "big.txt")
        with open(big, "w") as f:
            f.write("plain text line\n" * 40000)  # well over a pipe buffer
        r = subprocess.run([os.path.join(REPO, "scripts", "check-secrets.sh"), big], cwd=d,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           restore_signals=False, timeout=120)
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)


if __name__ == "__main__":
    unittest.main()
