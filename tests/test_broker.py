"""Tests for the broker transport (docs/design/broker.md). Agents run on
localhost under separate homes; no real claude, no network beyond loopback."""

import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

from cxd.broker import identity as ident  # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl binary")
class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxd-broker-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, name):
        return ident.ensure_identity(os.path.join(self.tmp, name))

    def handshake(self, server_id, trust, client_ctx):
        """One TLS connection on loopback: (server saw node id or error, client result)."""
        srv = socket.socket()
        srv.settimeout(10)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        out = {}

        def serve():
            try:
                conn, _ = srv.accept()
                conn.settimeout(10)
                s = ident.server_context(server_id, trust).wrap_socket(conn, server_side=True)
                out["server"] = ident.peer_node_id(s)
                s.sendall(b"ok\n")
                s.close()
            except (OSError, ssl.SSLError) as e:
                out["server"] = "refused: %s" % type(e).__name__
            finally:
                srv.close()

        t = threading.Thread(target=serve)
        t.start()
        try:
            c = client_ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=10))
            out["client_saw"] = ident.peer_node_id(c)
            out["client_read"] = c.recv(10)
            c.close()
        except (OSError, ssl.SSLError) as e:
            out["client_saw"] = "failed: %s" % type(e).__name__
        t.join(15)
        return out

    def test_identity_is_created_once_with_a_private_key(self):
        a = self.make("a")
        self.assertEqual(os.stat(a.key).st_mode & 0o777, 0o600)
        self.assertEqual(len(a.node_id), 64)
        again = ident.ensure_identity(a.dir)
        self.assertEqual(again.node_id, a.node_id)
        subject = subprocess.run(["openssl", "x509", "-in", a.cert, "-noout", "-subject"],
                                 stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertIn("cxd-", subject)
        self.assertNotEqual(self.make("b").node_id, a.node_id)

    def test_members_pinned_through_one_bundle_all_verify(self):
        a, b, c = self.make("a"), self.make("b"), self.make("c")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem, b.cert_pem, c.cert_pem])
        for member in (b, c):
            out = self.handshake(a, trust, ident.client_context(member, trust))
            self.assertEqual(out["server"], member.node_id, out)
            self.assertEqual(out["client_saw"], a.node_id, out)
            self.assertEqual(out["client_read"], b"ok\n")

    def test_a_joiner_without_a_certificate_gets_through_marked_as_such(self):
        a = self.make("a")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        out = self.handshake(a, trust, ident.join_client_context())
        self.assertIsNone(out["server"])  # no client certificate: join-only
        self.assertEqual(out["client_saw"], a.node_id)  # what the join code binds

    def test_a_stranger_is_refused(self):
        a, stranger = self.make("a"), self.make("stranger")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        client = ident.client_context(stranger, os.path.join(self.tmp, "trust.pem"))
        out = self.handshake(a, trust, client)
        self.assertTrue(str(out["server"]).startswith("refused"), out)

    def test_a_certificate_signed_by_a_member_passes_tls_but_not_the_pin(self):
        """GUARD pin-node-id. Mutation: TLS verification alone admits a peer ->
        anyone holding a certificate signed by a member's key gets in."""
        a = self.make("a")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        # A leaf certificate signed with member a's key (a is a CA).
        leaf_key, leaf_csr, leaf = (os.path.join(self.tmp, n) for n in ("leaf.key", "leaf.csr", "leaf.pem"))
        cnf = os.path.join(self.tmp, "leaf.cnf")
        with open(cnf, "w") as f:
            f.write("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=cxd-forged\n")
        for cmd in (["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", leaf_key],
                    ["openssl", "req", "-new", "-key", leaf_key, "-out", leaf_csr, "-config", cnf],
                    ["openssl", "x509", "-req", "-in", leaf_csr, "-CA", a.cert, "-CAkey", a.key,
                     "-CAcreateserial", "-out", leaf, "-days", "30"]):
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.tmp)
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        client.load_cert_chain(leaf, leaf_key)
        out = self.handshake(a, trust, client)
        forged_id = out["server"]
        self.assertEqual(len(str(forged_id)), 64, out)  # TLS let it through...
        self.assertFalse(ident.is_member(forged_id, {a.node_id}))  # ...the pin does not
        self.assertTrue(ident.is_member(a.node_id, {a.node_id}))
        self.assertFalse(ident.is_member(None, {a.node_id}))


if __name__ == "__main__":
    unittest.main()
