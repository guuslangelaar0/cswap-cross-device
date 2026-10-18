"""Transports: how a request reaches the host that owns a run.

Every operation is a host-side function in state.OPS, addressed by name with
a JSON-able args dict. A Transport carries {op, args} to the host and brings
back either a result, a CxdError (the host refused), or a TransportError (the
channel failed, which says NOTHING about the run).

    Transport.call(op, args)    -> result dict
    Transport.stream(op, args)  -> iterator of message dicts (watch)

P1 ships LocalTransport and SshTransport. P2 adds a broker transport behind
this same interface (and an auto transport that prefers it and falls back to
SSH); callers never change.
"""

import json
import os
import select
import shlex
import subprocess
import time

from .common import CxdError, PROTO, TransportError, load_config, self_names


class Transport(object):
    kind = "abstract"

    def __init__(self, host):
        self.host = host

    def call(self, op, args, timeout=None):  # pragma: no cover
        raise NotImplementedError

    def stream(self, op, args):  # pragma: no cover
        raise NotImplementedError

    def describe(self):
        return "%s(%s)" % (self.kind, self.host)


class LocalTransport(Transport):
    kind = "local"

    def call(self, op, args, timeout=None):
        from . import state
        fn = state.OPS.get(op)
        if fn is None:
            raise CxdError("unknown op %r" % op, "usage")
        return fn(args)

    def stream(self, op, args):
        from . import state
        if op != "stream":
            raise CxdError("unknown stream op %r" % op, "usage")
        for msg in state.stream(args):
            yield msg


class SshTransport(Transport):
    """One ssh invocation per request, running `cxd _rpc` on the host.

    ssh exits 255 on its own errors (unreachable host, auth, dropped link);
    `cxd _rpc` always exits 0 and prints exactly one JSON reply (or a stream
    of JSON lines ending in an 'end' message). Anything else is a transport
    failure.
    """
    kind = "ssh"

    def __init__(self, host, alias=None, python="python3",
                 cxd="$HOME/.cxd/bin/cxd", connect_timeout=10):
        Transport.__init__(self, host)
        self.alias = alias or host
        self.python = python
        self.cxd = cxd
        self.connect_timeout = connect_timeout

    def _argv(self):
        ssh = os.environ.get("CXD_SSH", "ssh")
        remote = "%s %s _rpc" % (self.python, _remote_quote(self.cxd))
        return [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % self.connect_timeout,
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                self.alias, remote]

    def call(self, op, args, timeout=None):
        req = json.dumps({"v": PROTO, "op": op, "args": args})
        try:
            r = subprocess.run(self._argv(), input=req, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout or 300)
        except subprocess.TimeoutExpired:
            raise TransportError("ssh %s: no reply within %ss" % (self.alias, timeout or 300))
        except OSError as e:
            raise TransportError("ssh %s: %s" % (self.alias, e))
        reply = _last_json_line(r.stdout)
        if r.returncode != 0 or reply is None:
            raise TransportError("ssh %s rc=%s: %s" % (self.alias, r.returncode, _short(r.stderr or r.stdout)))
        if not reply.get("ok"):
            raise CxdError(reply.get("error") or "remote error", reply.get("code") or "error")
        return reply.get("result")

    def stream(self, op, args):
        req = json.dumps({"v": PROTO, "op": op, "args": args}) + "\n"
        stall = float(args.get("keepalive", 10)) * 3 + 15
        try:
            p = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            raise TransportError("ssh %s: %s" % (self.alias, e))
        ended = False
        try:
            p.stdin.write(req.encode("utf-8"))
            p.stdin.close()
            buf = b""
            last = time.time()
            while True:
                r, _, _ = select.select([p.stdout], [], [], 1.0)
                if not r:
                    if time.time() - last > stall:
                        raise TransportError("ssh %s: stream stalled for %.0fs" % (self.alias, stall))
                    continue
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    break
                last = time.time()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = _parse_line(line)
                    if msg is None:
                        continue
                    if msg.get("type") == "error":
                        raise CxdError(msg.get("error") or "remote error", msg.get("code") or "error")
                    yield msg
                    if msg.get("type") == "end":
                        ended = True
                        return
        finally:
            if p.poll() is None:
                p.kill()
            try:
                err = p.stderr.read().decode("utf-8", "replace")
            except Exception:
                err = ""
            rc = p.wait()
        if not ended:
            raise TransportError("ssh %s stream closed rc=%s: %s" % (self.alias, rc, _short(err)))


def _remote_quote(s):
    # Keep $HOME expandable on the remote shell, quote everything else.
    if s.startswith("$HOME/"):
        return '"$HOME"/' + shlex.quote(s[len("$HOME/"):])
    return shlex.quote(s)


def _parse_line(line):
    line = line.strip()
    if not line.startswith(b"{"):
        return None
    try:
        v = json.loads(line.decode("utf-8", "replace"))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def _last_json_line(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
                if isinstance(v, dict) and "ok" in v:
                    return v
            except ValueError:
                return None
    return None


def _short(s, n=240):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "..."


def get_transport(host, cfg=None):
    cfg = cfg or load_config()
    h = (host or "").strip()
    if not h:
        raise CxdError("no host given", "usage")
    if h.lower() in self_names(cfg):
        return LocalTransport(h)
    hc = (cfg.get("hosts") or {}).get(h) or {}
    kind = hc.get("transport", "ssh")
    if kind == "local":
        return LocalTransport(h)
    if kind == "ssh":
        kw = {k: hc[k] for k in ("alias", "python", "cxd", "connect_timeout") if k in hc}
        return SshTransport(h, **kw)
    raise CxdError("host %s: transport %r is not available in this version" % (h, kind), "usage")


def known_hosts(cfg=None):
    cfg = cfg or load_config()
    from .common import self_label
    hosts = [self_label(cfg)]
    for h in (cfg.get("hosts") or {}):
        if h not in hosts and h.lower() not in self_names(cfg):
            hosts.append(h)
    return hosts
