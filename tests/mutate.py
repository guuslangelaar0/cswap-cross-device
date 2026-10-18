#!/usr/bin/env python3
"""Prove each guard can go red: apply one source mutation to a throwaway copy
of the repo, run the guard's tests there, and require them to FAIL. The
unmutated copy must pass the same tests.

    python3 tests/mutate.py            # all mutations
    python3 tests/mutate.py keeper     # one
"""

import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

T = "tests.test_cxd."
MUTATIONS = [
    {
        "name": "keeper",
        "what": "keeper missing: the supervisor never starts a keeper, so the first send's close is EOF",
        "edits": [("lib/cxd/supervise.py",
                   "        keeper = _start_keeper(d, fifo)\n",
                   "        keeper = subprocess.Popen(['true'])  # MUTATION: no keeper holds the FIFO\n        keeper.wait()\n")],
        "tests": [T + "TestRuns.test_keeper_holds_session_open_between_messages"],
        "expect": "red",
    },
    {
        "name": "send-dead",
        "what": "send to a dead run succeeds: liveness check removed AND the FIFO opened O_RDWR",
        "edits": [("lib/cxd/state.py", "        if not _run_accepts_input(ref):  # GUARD:send-alive\n",
                   "        if False:  # MUTATION\n"),
                  ("lib/cxd/state.py", "        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)  # GUARD:fifo-open\n",
                   "        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # MUTATION\n")],
        "tests": [T + "TestRuns.test_send_to_dead_run_is_refused"],
        "expect": "red",
    },
    {
        "name": "send-dead-liveness-only",
        "what": "defence in depth: removing ONLY the liveness check must stay green (the FIFO open still refuses)",
        "edits": [("lib/cxd/state.py", "        if not _run_accepts_input(ref):  # GUARD:send-alive\n",
                   "        if False:  # MUTATION\n")],
        "tests": [T + "TestRuns.test_send_to_dead_run_is_refused"],
        "expect": "green",
    },
    {
        "name": "watch-transport",
        "what": "an ssh failure is read as an exit: watch reports exit when the channel fails",
        "edits": [("lib/cxd/cli.py", "        except TransportError as e:  # GUARD:watch-transport\n",
                   "        except TransportError as e:  # GUARD:watch-transport\n"
                   "            _out(json.dumps({'ev': 'exit', 'reason': 'MUTATION ' + str(e)}) if a.json else 'EXITED')\n"
                   "            return EXIT_OK\n")],
        "tests": [T + "TestWatch.test_watch_survives_ssh_failures_without_reporting_exit"],
        "expect": "red",
    },
    {
        "name": "exit-rc",
        "what": "watch reports a run's exit before its supervisor has recorded the rc (rc=None)",
        "edits": [("lib/cxd/state.py", "            if not ex and supervisor_alive(pids):  # GUARD:exit-rc\n",
                   "            if False:  # MUTATION\n")],
        "tests": [T + "TestExitRecord.test_watch_waits_for_the_recorded_exit_code"],
        "expect": "red",
    },
    {
        "name": "exit-rc-cap",
        "what": "watch waits forever for a supervisor that never records the exit",
        "edits": [("lib/cxd/state.py", "                if waited < EXIT_RECORD_WAIT:  # GUARD:exit-rc-cap\n",
                   "                if True:  # MUTATION\n")],
        "tests": [T + "TestExitRecord.test_wait_is_capped_when_the_supervisor_hangs"],
        "expect": "red",
    },
    {
        "name": "exit-wait-gen",
        "what": "a wait timer left from an earlier gen cuts this gen's wait short (rc=None)",
        "edits": [("lib/cxd/state.py",
                   '                if not (isinstance(wait, dict) and wait.get("gen") == gen):\n',
                   '                if not isinstance(wait, dict):  # MUTATION\n')],
        "tests": [T + "TestExitRecord.test_a_wait_left_from_an_earlier_gen_does_not_cut_this_one"],
        "expect": "red",
    },
    {
        "name": "supervise-record",
        "what": "an unexpected supervisor failure leaves no exit record and an orphaned keeper",
        "edits": [("lib/cxd/supervise.py", "    except BaseException as e:  # GUARD:supervise-record\n",
                   "    except ZeroDivisionError as e:  # MUTATION\n")],
        "tests": [T + "TestSupervisorFailure.test_unexpected_failure_before_claude_records_an_exit_and_ends_the_keeper"],
        "expect": "red",
    },
    {
        "name": "stop-alive",
        "what": "stop leaves the process alive: signals are never delivered",
        "edits": [("lib/cxd/common.py", "        os.kill(int(pid), sig)\n", "        pass  # MUTATION: no signal\n")],
        "tests": [T + "TestRuns.test_stop_by_eof", T + "TestRuns.test_stop_escalates_to_sigint_and_sigterm"],
        "expect": "red",
    },
    {
        "name": "resume-alive",
        "what": "resume runs while the process is alive: the alive check is removed",
        "edits": [("lib/cxd/state.py", "    if claude_alive(pids):  # GUARD:resume-alive\n", "    if False:  # MUTATION\n")],
        "tests": [T + "TestRuns.test_resume_refused_while_alive"],
        "expect": "red",
    },
    {
        "name": "no-account-needs-cswap",
        "what": "a run without an account still goes through cswap",
        "edits": [("lib/cxd/supervise.py", "    if account:\n        launcher = find_cswap()\n",
                   "    if True:  # MUTATION\n        launcher = find_cswap()\n")],
        "tests": [T + "TestRuns.test_start_without_account_runs_plain_claude"],
        "expect": "red",
    },
    {
        "name": "default-account-ignored",
        "what": "default_account in config.json is ignored",
        "edits": [("lib/cxd/state.py",
                   'account = args.get("account") or load_config().get("default_account") or None',
                   'account = args.get("account") or None  # MUTATION')],
        "tests": [T + "TestRuns.test_start_uses_default_account_from_config"],
        "expect": "red",
    },
    {
        "name": "bypass-on-by-default",
        "what": "every headless run skips permission prompts again, without an opt-in",
        "edits": [("lib/cxd/state.py", '    "--verbose", "--replay-user-messages",\n]\n',
                   '    "--verbose", "--replay-user-messages", "--dangerously-skip-permissions",  # MUTATION\n]\n')],
        "tests": [T + "TestRuns.test_default_launch_has_no_permission_bypass"],
        "expect": "red",
    },
    {
        "name": "resume-bypass",
        "what": "resume adds --dangerously-skip-permissions without an opt-in",
        "edits": [("lib/cxd/state.py",
                   '    opt_in = meta.get("bypass_opt_in") is True or args.get("skip_permissions") is True  # GUARD:resume-bypass\n',
                   '    opt_in = True  # MUTATION\n')],
        "tests": [T + "TestRuns.test_resume_opts_in_to_bypass_only_when_asked"],
        "expect": "red",
    },
    {
        "name": "withdraw-now",
        "what": "a withdrawal is saved only after a good launch (a failed resume keeps the opt-in)",
        "edits": [("lib/cxd/state.py", '        meta["bypass_opt_in"] = False  # GUARD:withdraw-now\n',
                   "        pass  # MUTATION\n")],
        "tests": [T + "TestRuns.test_a_withdrawal_is_kept_even_if_the_resume_fails"],
        "expect": "red",
    },
    {
        "name": "legacy-opt-in",
        "what": "cxd 0.1.0's meta skip_permissions (flag OR host setting) is trusted as the run's opt-in",
        "edits": [("lib/cxd/state.py",
                   '    opt_in = meta.get("bypass_opt_in") is True or args.get("skip_permissions") is True  # GUARD:resume-bypass\n',
                   '    opt_in = meta.get("bypass_opt_in") is True or meta.get("skip_permissions") is True or args.get("skip_permissions") is True  # MUTATION\n')],
        "tests": [T + "TestRuns.test_resume_opts_in_to_bypass_only_when_asked"],
        "expect": "red",
    },
    {
        "name": "host-not-kept",
        "what": "start saves the host's skip_permissions as the run's own opt-in",
        "edits": [("lib/cxd/state.py",
                   '        "name": name, "host": self_label(), "account": account, "bypass_opt_in": opt_in,  # GUARD:host-not-kept\n',
                   '        "name": name, "host": self_label(), "account": account, "bypass_opt_in": bypass_opted_in(opt_in),  # MUTATION\n'),
                  ("lib/cxd/state.py", '    meta["bypass_opt_in"] = opt_in\n',
                   '    meta["bypass_opt_in"] = bypass_opted_in(opt_in)  # MUTATION\n')],
        "tests": [T + "TestRuns.test_host_bypass_at_start_is_not_kept_by_the_run"],
        "expect": "red",
    },
    {
        "name": "bypass-not-applied",
        "what": "a requested --skip-permissions the host did not apply goes unreported (exit 0)",
        "edits": [("lib/cxd/cli.py",
                   '    if a.skip_permissions and not res.get("skip_permissions"):  # GUARD:bypass-not-applied\n',
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestRuns.test_opt_in_a_host_ignores_is_an_error"],
        "expect": "red",
    },
    {
        "name": "bypass-json-true",
        "what": "any truthy skip_permissions (e.g. the string \"false\") opts in to the bypass",
        "edits": [("lib/cxd/state.py",
                   '    return run_opt_in is True or load_config().get("skip_permissions") is True  # GUARD:bypass-json-true\n',
                   '    return bool(run_opt_in) or bool(load_config().get("skip_permissions"))  # MUTATION\n')],
        "tests": [T + "TestRuns.test_resume_honours_the_host_bypass_setting",
                  T + "TestRuns.test_permission_bypass_is_opt_in_by_flag_or_host_config"],
        "expect": "red",
    },
    {
        "name": "release-pin-sha",
        "what": "the marketplace pin's sha is not compared with the tag's commit",
        "edits": [("scripts/check-release-pin.py", "    if tagged != sha:  # GUARD:release-pin-sha\n",
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_sha_of_another_commit_fails"],
        "expect": "red",
    },
    {
        "name": "release-pin-git-env",
        "what": "the pin check passes an inherited GIT_DIR on to git (checks the wrong repository)",
        "edits": [("scripts/check-release-pin.py",
                   '    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GUARD:release-pin-git-env\n',
                   "    env = dict(os.environ)  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_the_check_ignores_a_git_dir_it_inherits"],
        "expect": "red",
    },
    {
        "name": "release-pin-latest",
        "what": "the marketplace may pin an older release than the newest tag",
        "edits": [("scripts/check-release-pin.py",
                   "    if newest and release_version(ref) != newest:  # GUARD:release-pin-latest\n",
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_an_older_release_fails"],
        "expect": "red",
    },
    {
        "name": "release-pin-merged",
        "what": "the marketplace may pin a release tagged on an unmerged commit",
        "edits": [("scripts/check-release-pin.py", "    if rc != 0:  # GUARD:release-pin-merged\n",
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_a_tag_on_an_unmerged_commit_fails"],
        "expect": "red",
    },
    {
        "name": "release-pin-version",
        "what": "the pinned tag's plugin.json version is not compared with the tag",
        "edits": [("scripts/check-release-pin.py",
                   '    if manifest.get("version") != ref[1:]:  # GUARD:release-pin-version\n',
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_tag_whose_manifest_has_another_version_fails"],
        "expect": "red",
    },
    {
        "name": "release-pin-https",
        "what": "the marketplace may install over ssh (needs a GitHub key)",
        "edits": [("scripts/check-release-pin.py",
                   '    if not isinstance(url, str) or not url.startswith("https://"):  # GUARD:release-pin-https\n',
                   "    if False:  # MUTATION\n")],
        "tests": [T + "TestReleasePin.test_url_must_be_https"],
        "expect": "red",
    },
    {
        "name": "ack-after-write",
        "what": "a claimed message counts as delivered even when the hook could not write it",
        "edits": [("lib/cxd/hook.py",
                   "            return 0  # not handed over: no ack, so the messages come again\n",
                   "            ack(sid, claims, event); return 0  # MUTATION\n")],
        "tests": [T + "TestHooks.test_message_a_hook_could_not_hand_over_is_delivered_by_the_next"],
        "expect": "red",
    },
    {
        "name": "redeliver",
        "what": "messages claimed by a hook that died are never requeued",
        "edits": [("lib/cxd/hook.py",
                   '            os.rename(os.path.join(claimed, n), os.path.join(inbox, mid + ".json"))  # GUARD:redeliver\n',
                   "            pass  # MUTATION\n")],
        "tests": [T + "TestHooks.test_claim_of_a_dead_hook_is_requeued",
                  T + "TestHooks.test_message_a_hook_could_not_hand_over_is_delivered_by_the_next"],
        "expect": "red",
    },
    {
        "name": "claim-identity",
        "what": "a claim is judged alive by its pid alone (a recycled pid keeps it forever)",
        "edits": [("lib/cxd/hook.py",
                   '    return claim_token(pid) == "%d-%s" % (pid, digest)  # GUARD:claim-identity\n',
                   "    return proc_identity(pid) is not None  # MUTATION\n")],
        "tests": [T + "TestHooks.test_claim_of_a_recycled_pid_is_requeued"],
        "expect": "red",
    },
    {
        "name": "init-no-link",
        "what": "cxd init does not link ~/.cxd/bin/cxd, so peers cannot reach this host",
        "edits": [("lib/cxd/cli.py", "    link, target = _link_cli()\n",
                   "    link, target = '-', '-'  # MUTATION\n")],
        "tests": [T + "TestInit.test_init_writes_config_and_links_cli"],
        "expect": "red",
    },
]


def copy_repo(dst):
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".tmp-tests"))


def run_tests(root, tests):
    r = subprocess.run([sys.executable, "-m", "unittest", "-v"] + tests, cwd=root,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=600)
    return r.returncode, r.stdout


def main(names):
    chosen = [m for m in MUTATIONS if not names or m["name"] in names]
    failures = 0
    for m in chosen:
        print("=" * 78)
        print("MUTATION %s: %s" % (m["name"], m["what"]))
        base = tempfile.mkdtemp(prefix="cxd-mut-")
        try:
            clean = os.path.join(base, "clean")
            copy_repo(clean)
            rc, out = run_tests(clean, m["tests"])
            print("-- unmutated: rc=%d %s" % (rc, "GREEN" if rc == 0 else "RED"))
            print(_summary(out))
            mutant = os.path.join(base, "mutant")
            copy_repo(mutant)
            for rel, old, new in m["edits"]:
                p = os.path.join(mutant, rel)
                s = open(p).read()
                n = s.count(old)
                if n != 1:
                    print("!! mutation anchor found %d times in %s: %r" % (n, rel, old))
                    failures += 1
                    break
                open(p, "w").write(s.replace(old, new))
            else:
                rc2, out2 = run_tests(mutant, m["tests"])
                state = "GREEN" if rc2 == 0 else "RED"
                print("-- mutated:   rc=%d %s (expected %s)" % (rc2, state, m["expect"].upper()))
                print(_summary(out2, failures_only=True))
                ok = rc == 0 and ((rc2 != 0) if m["expect"] == "red" else (rc2 == 0))
                print("-> %s" % ("PASS (guard proven)" if ok else "FAIL"))
                failures += 0 if ok else 1
        finally:
            shutil.rmtree(base, ignore_errors=True)
    print("=" * 78)
    print("%d mutation(s), %d failure(s)" % (len(chosen), failures))
    return 1 if failures else 0


def _summary(out, failures_only=False):
    keep = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.endswith(("... ok", "... FAIL", "... ERROR")) or line.startswith(("Ran ", "OK", "FAILED")):
            keep.append("   " + line)
        elif failures_only and (line.startswith("AssertionError") or '"ev": "exit"' in line or "Error:" in line):
            keep.append("   " + line[:300])
    return "\n".join(keep)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
