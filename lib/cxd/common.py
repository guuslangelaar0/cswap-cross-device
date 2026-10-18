"""Shared helpers: paths, atomic JSON files, process identity, environment.

Python 3.9+ stdlib only.
"""

import json
import os
import sys
import time

# subprocess, shutil, random and re are imported lazily: the hook path must
# start fast (it runs after every tool call in every session).

VERSION = "0.1.0"
PROTO = 1

_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz" "0123456789" "._-")


class _Matcher(object):
    """re-free validator with the .match() shape of a compiled pattern."""

    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi

    def match(self, s):
        return (isinstance(s, str) and self.lo <= len(s) <= self.hi and s[0].isalnum()
                and s[0].isascii() and all(c in _SAFE for c in s))


NAME_RE = _Matcher(1, 64)   # run names
SID_RE = _Matcher(4, 128)   # session ids

# Exit codes used by the CLI.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_TRANSPORT = 4
EXIT_NOT_APPLIED = 5  # the run started, but the permission choice asked for was not applied


class CxdError(Exception):
    """An operation was refused or failed on the host that ran it."""

    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


class TransportError(Exception):
    """The channel to a host failed. Says nothing about the run's state."""


# ---------------------------------------------------------------- paths

def cxd_home():
    return os.environ.get("CXD_HOME") or os.path.join(os.path.expanduser("~"), ".cxd")


def path(*parts):
    return os.path.join(cxd_home(), *parts)


def ensure_dir(p):
    """mkdir -p with mode 700 on every level it creates."""
    if os.path.isdir(p):
        return p
    missing = []
    cur = os.path.abspath(p)
    while not os.path.isdir(cur):
        missing.append(cur)
        cur = os.path.dirname(cur)
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)
    return p


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cxd_bin():
    return os.path.join(repo_root(), "bin", "cxd")


# ---------------------------------------------------------------- files

def read_json(p, default=None):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write_json(p, obj):
    tmp = "%s.tmp.%d.%s" % (p, os.getpid(), os.urandom(4).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(obj, indent=1, sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, p)


def append_jsonl(p, obj):
    line = (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def excerpt(text, n=300):
    if text is None:
        return ""
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------- processes

def _read_identity(pid):
    """("ok", identity) for a live, non-zombie pid; ("dead", None) when it
    provably is not running (no such process, or a zombie); ("unknown", None)
    when it could not be checked (an unreadable /proc, a `ps` that failed or
    timed out, a pid that is not a number)."""
    try:
        pid = int(pid)
        if pid <= 0:
            return "unknown", None
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        pass  # it exists, under another user
    except (OSError, ValueError, TypeError, OverflowError):
        return "unknown", None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % pid, "r") as f:
                data = f.read()
        except FileNotFoundError:
            return "dead", None
        except OSError:
            return "unknown", None
        rest = data.rsplit(")", 1)[-1].split()
        if len(rest) < 20:
            return "unknown", None
        if rest[0] in ("Z", "X"):
            return "dead", None
        return "ok", "linux:" + rest[19]
    import subprocess  # only off Linux: the hook path stays light
    try:
        r = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, env=dict(os.environ, LC_ALL="C"), timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    line = r.stdout.strip()
    if not line:
        return ("dead", None) if r.returncode != 0 else ("unknown", None)
    stat, _, lstart = line.partition(" ")
    if stat.startswith("Z"):
        return "dead", None
    return "ok", "ps:" + " ".join(lstart.split())


def proc_identity(pid):
    """A stable identity for a live, non-zombie pid (its start time), or None.

    Recording this next to a pid lets cxd prove a pid is still the process it
    started before it signals it: a recycled pid has another start time.
    """
    if not pid:
        return None
    status, ident = _read_identity(pid)
    return ident if status == "ok" else None


def proc_state(pid, ident):
    """"alive" (pid is still the recorded process), "dead" (provably not: no
    such process, a zombie, or another process now has that pid), or
    "unknown" (nothing recorded, or it could not be checked). Destructive
    decisions (pruning) act only on "dead"."""
    if not pid or not ident:
        return "unknown"
    status, cur = _read_identity(pid)
    if status == "ok":
        return "alive" if cur == ident else "dead"
    return status  # GUARD:prune-tristate


def proc_alive(pid, ident):
    """True only if pid is alive AND is the process we recorded."""
    if not pid or not ident:
        return False
    return proc_identity(pid) == ident


def proc_parent_and_argv(pid):
    """(ppid, [argv...]) for pid, or (None, None). On Linux argv is exact
    (/proc/<pid>/cmdline); elsewhere it comes from `ps -ww -o args=`, split on
    whitespace (-ww: BSD ps truncates args without a terminal)."""
    import subprocess
    pid = int(pid)
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % pid, "r") as f:
                ppid = int(f.read().rsplit(")", 1)[-1].split()[1])
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read()
        except (OSError, ValueError, IndexError):
            return None, None
        argv = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")] if raw else []
        if len(argv) == 1 and " " in argv[0]:
            argv = argv[0].split()  # a process that rewrote its title into argv[0]
        return ppid, argv
    try:
        r = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,args=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, env=dict(os.environ, LC_ALL="C"), timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    line = r.stdout.strip()
    if not line:
        return None, None
    ppid, _, args = line.partition(" ")
    try:
        return int(ppid), args.split()
    except ValueError:
        return None, None


def proc_parent_and_args(pid):
    """(ppid, args as one string) for pid, or (None, None)."""
    ppid, argv = proc_parent_and_argv(pid)
    return (ppid, " ".join(argv)) if argv is not None else (None, None)


def signal_verified(pid, ident, sig):
    """Send sig to pid only if it is still the process cxd recorded."""
    if not proc_alive(pid, ident):
        return False
    try:
        os.kill(int(pid), sig)
        return True
    except ProcessLookupError:
        return False


def wait_dead(pid, ident, timeout, step=0.2):
    deadline = time.time() + timeout
    while True:
        if not proc_alive(pid, ident):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(step)


# ---------------------------------------------------------------- environment

# Variables a lead session leaks into child processes. A worker started from
# inside a lead's Bash tool must not inherit the lead's session identity,
# messaging socket or plugin paths.
_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PLUGIN_", "CODEX_COMPANION_")
_SCRUB_NAMES = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR",
                "CLAUDE_EFFORT", "AI_AGENT")


def augmented_path(env=None):
    env = os.environ if env is None else env
    p = env.get("PATH", "/usr/bin:/bin")
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    parts = p.split(os.pathsep)
    if local_bin not in parts and os.path.isdir(local_bin):
        p = local_bin + os.pathsep + p
    return p


def worker_env(extra=None):
    env = {}
    for k, v in os.environ.items():
        if k in _SCRUB_NAMES or k.startswith(_SCRUB_PREFIXES):
            continue
        env[k] = v
    env["PATH"] = augmented_path(env)
    if extra:
        env.update(extra)
    return env


def find_cswap():
    import shutil
    explicit = os.environ.get("CXD_CSWAP")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("cswap", path=augmented_path())


def find_claude():
    import shutil
    explicit = os.environ.get("CXD_CLAUDE")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("claude", path=augmented_path())


# ---------------------------------------------------------------- config

def load_config():
    """~/.cxd/config.json, written by `cxd init`:

        self             names this machine answers to (first one is its label)
        hosts            {name: {transport, alias, python, cxd, connect_timeout}}
        accounts         cswap accounts on this machine (checked by `cxd doctor`)
        default_account  cswap account `cxd start` uses when --account is omitted;
                         unset means plain `claude` (no cswap)
    """
    cfg = read_json(path("config.json"), {}) or {}
    cfg.setdefault("self", [])
    cfg.setdefault("hosts", {})
    cfg.setdefault("accounts", [])
    cfg.setdefault("default_account", None)
    return cfg


def self_names(cfg=None):
    cfg = cfg or load_config()
    names = {"local"}
    names.update(n.lower() for n in cfg.get("self", []))
    env_self = os.environ.get("CXD_SELF")
    if env_self:
        names.update(n.strip().lower() for n in env_self.split(",") if n.strip())
    try:
        import socket
        names.add(socket.gethostname().split(".")[0].lower())
    except Exception:
        pass
    return names


def self_label(cfg=None):
    cfg = cfg or load_config()
    if os.environ.get("CXD_SELF"):
        return os.environ["CXD_SELF"].split(",")[0].strip()
    if cfg.get("self"):
        return cfg["self"][0]
    import socket
    return socket.gethostname().split(".")[0].lower()
