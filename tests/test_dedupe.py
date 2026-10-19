"""Tests for the claim store (lib/cxd/dedupe.py; docs/design/broker.md,
Delivery): every request id runs at most once, whatever arrives when."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

from cxd import dedupe  # noqa: E402


def rid(n=1):
    return "%032x" % n


class TestClaims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxd-dedupe-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "a", "home")  # an escape would land inside self.tmp
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"CXD_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)

    def test_a_request_runs_once_and_its_reply_is_kept(self):
        r = dedupe.arrive(rid(), "a" * 32, wait=30)
        self.assertEqual((r["state"], r["owner"]), (dedupe.AWAITING, "a" * 32))
        self.assertEqual(dedupe.go(rid(), "a" * 32)[0], "run")
        dedupe.finish(rid(), "a" * 32, reply={"ok": 1})
        for attempt in ("a" * 32, "b" * 32):  # the same attempt again, or a new one
            r = dedupe.arrive(rid(), attempt, wait=30)
            self.assertEqual((r["state"], r["reply"]), (dedupe.DONE, {"ok": 1}))
            self.assertEqual(dedupe.go(rid(), attempt)[0], "final")

    def test_an_error_is_kept_too(self):
        dedupe.arrive(rid(), "a" * 32, wait=30)
        dedupe.go(rid(), "a" * 32)
        dedupe.finish(rid(), "a" * 32, error={"code": "x", "message": "boom"})
        self.assertEqual(dedupe.current(rid())["state"], dedupe.ERROR)

    def test_a_waiting_attempt_takes_over_and_the_earlier_owner_cannot_run_it(self):
        """GUARD claim-go-owner. Mutation: go does not check the owner -> the
        broker copy and the ssh copy of one request both run it."""
        dedupe.arrive(rid(), "a" * 32, wait=30, seq=1)  # broker attempt
        r = dedupe.arrive(rid(), "b" * 32, wait=60, seq=2)  # the ssh fallback takes over
        self.assertEqual(r["owner"], "b" * 32)
        self.assertEqual(dedupe.go(rid(), "a" * 32)[0], "duplicate")
        self.assertEqual(dedupe.current(rid())["state"], dedupe.AWAITING)
        self.assertEqual(dedupe.go(rid(), "b" * 32)[0], "run")
        self.assertEqual(dedupe.go(rid(), "a" * 32)[0], "duplicate")

    def test_only_a_later_attempt_takes_over(self):
        """GUARD claim-seq. Mutation: any attempt takes over an awaiting claim ->
        a stale broker attempt that arrives after the ssh fallback takes the
        claim back, and the fallback's go is turned away."""
        dedupe.arrive(rid(), "a" * 32, wait=30, seq=2)  # the ssh fallback
        r = dedupe.arrive(rid(), "b" * 32, wait=30, seq=1)  # the broker attempt, late
        self.assertEqual(r["owner"], "a" * 32)
        self.assertEqual(dedupe.go(rid(), "b" * 32)[0], "duplicate")
        self.assertEqual(dedupe.go(rid(), "a" * 32)[0], "run")

    def test_pruning_waits_for_claims_in_progress(self):
        """GUARD claim-store-lock. Mutation: prune does not take the store lock
        -> it removes a lock file that another process holds or waits on, and
        two processes then lock different files for the same request."""
        old = time.time() - dedupe.KEEP_FINAL_SECONDS - 60
        dedupe.arrive(rid(), "a" * 32, wait=30, now=old)
        dedupe.go(rid(), "a" * 32, now=old)
        dedupe.finish(rid(), "a" * 32, reply={}, now=old)
        holding, released = threading.Event(), []

        def hold():
            with dedupe._Locked(rid()):
                holding.set()
                time.sleep(0.6)
                released.append(time.monotonic())
        t = threading.Thread(target=hold)
        t.start()
        holding.wait(5)
        self.assertEqual(dedupe.prune(), [rid()])
        done = time.monotonic()
        t.join(5)
        self.assertTrue(released and done >= released[0], "prune ran while a claim lock was held")

    def test_without_go_in_time_nothing_ever_runs(self):
        """GUARD claim-lapse. Mutation: the lapse deadline is ignored -> a go
        that arrives after the client gave up (exit 4: nothing ran) runs it."""
        t0 = time.time()
        r = dedupe.arrive(rid(), "a" * 32, wait=10, now=t0)
        self.assertAlmostEqual(r["lapse_at"], t0 + 10 + dedupe.LAPSE_SLACK, places=3)
        later = t0 + 10 + dedupe.LAPSE_SLACK + 1
        self.assertEqual(dedupe.go(rid(), "a" * 32, now=later)[0], "cancelled")
        self.assertEqual(dedupe.current(rid(), now=later)["state"], dedupe.CANCELLED)
        self.assertEqual(dedupe.arrive(rid(), "b" * 32, wait=30, now=later)["state"], dedupe.CANCELLED)

    def test_a_takeover_never_shortens_the_lapse(self):
        t0 = time.time()
        dedupe.arrive(rid(), "a" * 32, wait=60, now=t0)
        r = dedupe.arrive(rid(), "b" * 32, wait=1, now=t0 + 1)
        self.assertAlmostEqual(r["lapse_at"], t0 + 60 + dedupe.LAPSE_SLACK, places=3)

    def test_concurrent_attempts_run_a_request_once(self):
        """GUARD claim-lock. Mutation: changes are made without the claim's lock
        -> two attempts that arrive and go at the same time can both run it."""
        for n in range(40):
            runs, barrier = [], threading.Barrier(6)

            def attempt(i):
                me = "%032x" % (1000 + i)
                barrier.wait()
                dedupe.arrive(rid(n), me, wait=30, seq=i)
                if dedupe.go(rid(n), me)[0] == "run":
                    runs.append(me)
            threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertLessEqual(len(runs), 1, "request %d ran %d times" % (n, len(runs)))

    def test_a_dead_executor_makes_the_outcome_unknown(self):
        """GUARD claim-executor-dead. Mutation: a running claim is not checked
        for its executor -> a retry after the executor died waits forever."""
        code = ("import sys; sys.path.insert(0, %r); from cxd import dedupe; "
                "dedupe.arrive(%r, 'a' * 32, wait=30); print(dedupe.go(%r, 'a' * 32)[0])"
                % (os.path.join(REPO, "lib"), rid(), rid()))
        out = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, universal_newlines=True,
                             env=dict(os.environ, CXD_HOME=self.home), timeout=30).stdout
        self.assertEqual(out.strip(), "run")  # and then the executor exited without finishing
        self.assertEqual(dedupe.current(rid())["state"], dedupe.UNKNOWN)
        self.assertEqual(dedupe.wait_final(rid(), timeout=2)["state"], dedupe.UNKNOWN)

    def test_a_live_executor_is_waited_for(self):
        dedupe.arrive(rid(), "a" * 32, wait=30)
        dedupe.go(rid(), "a" * 32)  # this process is the executor
        self.assertIsNone(dedupe.wait_final(rid(), timeout=0.3))
        threading.Timer(0.3, dedupe.finish, (rid(), "a" * 32), {"reply": {"v": 2}}).start()
        self.assertEqual(dedupe.wait_final(rid(), timeout=10)["reply"], {"v": 2})

    def test_only_the_executor_finishes(self):
        dedupe.arrive(rid(), "a" * 32, wait=30)
        dedupe.go(rid(), "a" * 32)
        self.assertFalse(dedupe.finish(rid(), "b" * 32, reply={"x": 1}))
        self.assertEqual(dedupe.current(rid())["state"], dedupe.RUNNING)
        self.assertTrue(dedupe.finish(rid(), "a" * 32, reply={"x": 1}))

    def test_ids_are_hex_only(self):
        """GUARD claim-id. Mutation: ids are not checked -> a request id like
        '../../x' writes outside the store."""
        for bad in ("../../escape", "", "A" * 32, "a" * 31, None, "a" * 32 + "/x"):
            with self.assertRaises(ValueError, msg=bad):
                dedupe.arrive(bad, "a" * 32, wait=30)
            with self.assertRaises(ValueError, msg=bad):
                dedupe.arrive(rid(), bad, wait=30)
        self.assertEqual(os.listdir(self.tmp), ["a"])
        self.assertEqual(os.listdir(os.path.join(self.tmp, "a")), ["home"])

    def test_an_attempt_for_another_operation_is_refused(self):
        """GUARD claim-same-op. Mutation: the operation is not compared -> a
        --request-id given to `cxd stop` returns the recorded reply of a send
        as if the stop had happened."""
        dedupe.arrive(rid(), "a" * 32, wait=30, seq=1, op="send", digest="d1")
        for op, digest in (("stop", "d1"), ("send", "d2"), (None, None)):
            with self.assertRaises(dedupe.Mismatch):
                dedupe.arrive(rid(), "b" * 32, wait=30, seq=2, op=op, digest=digest)
        r = dedupe.arrive(rid(), "c" * 32, wait=30, seq=3, op="send", digest="d1")
        self.assertEqual(r["owner"], "c" * 32)

    def test_only_the_owner_cancels_and_only_before_go(self):
        """GUARD claim-cancel-owner. Mutation: any attempt's end of input
        cancels -> the broker attempt's client going away cancels the claim
        that the ssh fallback took over and is about to run."""
        dedupe.arrive(rid(), "a" * 32, wait=30, seq=1)
        dedupe.arrive(rid(), "b" * 32, wait=30, seq=2)  # taken over
        self.assertFalse(dedupe.cancel(rid(), "a" * 32))
        self.assertEqual(dedupe.current(rid())["state"], dedupe.AWAITING)
        self.assertTrue(dedupe.cancel(rid(), "b" * 32))
        self.assertEqual(dedupe.current(rid())["state"], dedupe.CANCELLED)
        dedupe.arrive(rid(2), "a" * 32, wait=30)
        dedupe.go(rid(2), "a" * 32)
        self.assertFalse(dedupe.cancel(rid(2), "a" * 32))  # running: too late to cancel
        self.assertFalse(dedupe.cancel(rid(3), "a" * 32))  # never arrived

    def test_ids_carry_their_creation_time(self):
        t = 1792390000.123
        r = dedupe.new_request_id(now=t)
        self.assertRegex(r, r"^[0-9a-f]{32}$")
        self.assertAlmostEqual(dedupe.id_time(r), t, places=2)
        self.assertNotEqual(dedupe.new_request_id(now=t), r)

    def test_a_record_is_pruned_only_when_its_id_is_old_too(self):
        """GUARD claim-prune-id-time. Mutation: only the final-state stamp counts
        -> after a clock jumped forward, a record pruned early lets a
        re-issued request run a second time."""
        t0 = time.time()
        young = dedupe.new_request_id(now=t0 - 3600)  # made an hour ago...
        old = dedupe.new_request_id(now=t0 - 40 * 3600)
        for r in (young, old):
            dedupe.arrive(r, "a" * 32, wait=30, now=t0 - 25 * 3600)
            dedupe.go(r, "a" * 32, now=t0 - 25 * 3600)
            dedupe.finish(r, "a" * 32, reply={}, now=t0 - 25 * 3600)  # ...but "done" 25 h ago by a jumped clock
        self.assertEqual(dedupe.prune(now=t0), [old])
        self.assertIsNotNone(dedupe.current(young))

    def test_only_old_final_records_are_pruned(self):
        t0 = time.time()
        old = t0 - dedupe.KEEP_FINAL_SECONDS - 60
        dedupe.arrive(rid(1), "a" * 32, wait=30, now=old)
        dedupe.go(rid(1), "a" * 32, now=old)
        dedupe.finish(rid(1), "a" * 32, reply={}, now=old)  # done a day ago
        dedupe.arrive(rid(2), "a" * 32, wait=30)
        dedupe.go(rid(2), "a" * 32)
        dedupe.finish(rid(2), "a" * 32, reply={})  # done just now
        dedupe.arrive(rid(3), "a" * 32, wait=30, now=old)
        dedupe.go(rid(3), "a" * 32, now=old)  # running a day later, executor alive
        self.assertEqual(dedupe.prune(now=t0), [rid(1)])
        self.assertIsNone(dedupe.current(rid(1)))
        self.assertEqual(dedupe.current(rid(2))["state"], dedupe.DONE)
        self.assertEqual(dedupe.current(rid(3))["state"], dedupe.RUNNING)
        self.assertEqual(sorted(n for n in os.listdir(os.path.join(self.home, "dedupe")) if rid(1) in n), [])


if __name__ == "__main__":
    unittest.main()
