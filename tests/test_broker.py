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
import time
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

    def handshake(self, server_id, trust, client_ctx, members=()):
        """One TLS connection on loopback: what the server saw (node id or error)
        and how it classified the peer, and what the client saw."""
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
                out["class"] = ident.classify(s, set(members))
                s.sendall(b"ok\n")
                s.close()
            except (OSError, ssl.SSLError) as e:
                out["server"] = "refused: %s" % type(e).__name__
            finally:
                srv.close()

        t = threading.Thread(target=serve)
        t.start()
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        c = None
        try:
            c = client_ctx.wrap_socket(raw)
            out["client_saw"] = ident.peer_node_id(c)
            out["client_read"] = c.recv(10)
        except (OSError, ssl.SSLError) as e:
            out.setdefault("client_saw", "failed: %s" % type(e).__name__)
        finally:
            (c or raw).close()
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

    def test_a_fresh_certificate_is_already_valid_for_a_peer_whose_clock_lags(self):
        a = self.make("a")
        start = subprocess.run(["openssl", "x509", "-in", a.cert, "-noout", "-startdate"],
                               stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        not_before = ssl.cert_time_to_seconds(start.split("=", 1)[1])
        self.assertLess(not_before, time.time() - 23 * 3600)

    def test_concurrent_first_use_makes_one_identity(self):
        """GUARD identity-lock. Mutation: no lock around check-and-create ->
        two first users each make a key and certificate; one of them gets an
        identity that is not the one on disk (or a key that is not its cert's)."""
        for round_ in range(3):
            d = os.path.join(self.tmp, "race%d" % round_)
            got, barrier = [], threading.Barrier(4)

            def first_use():
                barrier.wait()
                try:
                    got.append(ident.ensure_identity(d).node_id)
                except ident.IdentityError as e:
                    got.append("error: %s" % e)
            threads = [threading.Thread(target=first_use) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            self.assertEqual(got, [ident.Identity(d).node_id] * 4)

    def test_a_key_that_is_not_the_certificates_is_refused(self):
        """GUARD identity-pair. Mutation: the identity is loaded without checking
        that the key belongs to the certificate -> after a crash between the two
        writes the agent presents a certificate it cannot use."""
        a, b = self.make("a"), self.make("b")
        shutil.copy(b.key, a.key)
        with self.assertRaises(ident.IdentityError):
            ident.Identity(a.dir)
        with self.assertRaises(ident.IdentityError):
            ident.ensure_identity(a.dir)  # never silently a new identity

    def test_a_corrupt_certificate_is_an_identity_error(self):
        a = self.make("a")
        for junk in (b"", b"\xff\xfe not a certificate", b"-----BEGIN CERTIFICATE-----\n!!\n"):
            with open(a.cert, "wb") as f:
                f.write(junk)
            with self.assertRaises(ident.IdentityError):
                ident.ensure_identity(a.dir)

    def test_a_node_id_is_of_exactly_one_certificate(self):
        """GUARD one-cert. Mutation: a PEM with several certificates gets a node
        id (a hash of garbage) -> a join message carrying a bundle is accepted."""
        a, b = self.make("a"), self.make("b")
        self.assertEqual(ident.node_id_of_pem(a.cert_pem), a.node_id)
        # Refused for the count, not by luck: with two certificates, decoding
        # the base64 between the markers fails or not depending on padding.
        for bad in (a.cert_pem + b.cert_pem, b.cert_pem + a.cert_pem, "", "hello", None):
            with self.assertRaisesRegex(ident.IdentityError, "exactly one"):
                ident.node_id_of_pem(bad)
        with self.assertRaisesRegex(ident.IdentityError, "exactly one"):
            ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem + b.cert_pem])

    def test_the_trust_bundle_is_private_and_written_whole(self):
        a, b = self.make("a"), self.make("b")
        p = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem, b.cert_pem])
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(self.tmp).count("trust.pem"), 1)
        self.assertEqual([n for n in os.listdir(self.tmp) if ".tmp." in n], [])
        with open(p) as f:
            self.assertEqual(f.read().count("BEGIN CERTIFICATE"), 2)

    def test_contexts_need_a_trust_bundle(self):
        a = self.make("a")
        missing = os.path.join(self.tmp, "none.pem")
        for make in (ident.server_context, ident.client_context):
            with self.assertRaises(ident.IdentityError):
                make(a, missing)

    def test_members_pinned_through_one_bundle_all_verify(self):
        a, b, c = self.make("a"), self.make("b"), self.make("c")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem, b.cert_pem, c.cert_pem])
        for member in (b, c):
            out = self.handshake(a, trust, ident.client_context(member, trust),
                                 members=[a.node_id, b.node_id, c.node_id])
            self.assertEqual(out["server"], member.node_id, out)
            self.assertEqual(out["class"], ident.MEMBER)
            self.assertEqual(out["client_saw"], a.node_id, out)
            self.assertEqual(out["client_read"], b"ok\n")

    def test_a_joiner_without_a_certificate_gets_through_marked_as_such(self):
        a = self.make("a")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        out = self.handshake(a, trust, ident.join_client_context(), members=[a.node_id])
        self.assertIsNone(out["server"])  # no client certificate: join-only
        self.assertEqual(out["class"], ident.JOINER)
        self.assertEqual(out["client_saw"], a.node_id)  # what the join code binds

    def test_a_stranger_is_refused(self):
        a, stranger = self.make("a"), self.make("stranger")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        client = ident.client_context(stranger, os.path.join(self.tmp, "trust.pem"))
        out = self.handshake(a, trust, client)
        self.assertTrue(str(out["server"]).startswith("refused"), out)

    def test_a_member_refuses_a_stranger_server(self):
        """GUARD client-verify. Mutation: a member dialing does not verify the
        server -> it talks to any machine that answers on the address."""
        a, stranger = self.make("a"), self.make("stranger")
        mine = ident.write_trust(os.path.join(self.tmp, "mine.pem"), [a.cert_pem])
        theirs = ident.write_trust(os.path.join(self.tmp, "theirs.pem"), [stranger.cert_pem, a.cert_pem])
        out = self.handshake(stranger, theirs, ident.client_context(a, mine))
        self.assertTrue(str(out["client_saw"]).startswith("failed"), out)

    def test_only_aead_ciphers_with_ecdhe(self):
        """GUARD tls-ciphers. Mutation: the server takes the default cipher list
        -> a client can negotiate a CBC cipher."""
        a = self.make("a")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])

        def client(cipher):
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
            c.maximum_version = ssl.TLSVersion.TLSv1_2  # TLS 1.3 suites are not set_ciphers' to choose
            c.set_ciphers(cipher)
            return c
        out = self.handshake(a, trust, client("ECDHE-ECDSA-AES128-SHA256"))
        self.assertTrue(str(out["client_saw"]).startswith("failed"), out)
        out = self.handshake(a, trust, client("ECDHE-ECDSA-AES128-GCM-SHA256"))
        self.assertEqual(out["client_saw"], a.node_id, out)

    def test_tls_1_2_is_the_floor(self):
        """GUARD tls-floor. Mutation: the contexts allow TLS 1.0."""
        a = self.make("a")
        trust = ident.write_trust(os.path.join(self.tmp, "trust.pem"), [a.cert_pem])
        for ctx in (ident.server_context(a, trust), ident.client_context(a, trust), ident.join_client_context()):
            self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

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
        out = self.handshake(a, trust, client, members=[a.node_id])
        self.assertEqual(len(str(out["server"])), 64, out)  # TLS let it through...
        self.assertIsNone(out["class"])  # ...the pin does not


class TestWire(unittest.TestCase):
    """Framing: one JSON object per line, with limits that hold before a
    single newline arrives."""

    def setUp(self):
        from cxd.broker import wire
        self.wire = wire

    def test_frames_round_trip_whatever_the_chunking(self):
        msgs = [{"t": "hello", "name": "été ✓", "n": 1}, {"t": "ping", "id": "x"}, {"t": "rep", "text": "a\nb"}]
        data = b"".join(self.wire.encode(m) for m in msgs)
        self.assertEqual(data.count(b"\n"), 3)  # a newline inside a value is escaped
        for size in (1, 2, 7, len(data)):
            r, got = self.wire.Reader(authenticated=True), []
            for i in range(0, len(data), size):
                got += r.feed(data[i:i + size])
            self.assertEqual(got, msgs, size)

    def test_encode_refuses_what_is_not_a_frame(self):
        for bad in ([], {"x": 1}, {"t": 5}, "hello"):
            with self.assertRaises(self.wire.FrameError):
                self.wire.encode(bad)
        with self.assertRaises(self.wire.FrameError):
            self.wire.encode({"t": "big", "s": "x" * self.wire.FRAME_LIMIT})

    def test_a_frame_without_its_newline_is_refused_once_it_is_too_long(self):
        """GUARD frame-limit. Mutation: only complete lines are measured -> a
        peer that never sends a newline fills the agent's memory."""
        r = self.wire.Reader()
        self.assertEqual(r.feed(b"{" + b" " * (self.wire.PRE_AUTH_LIMIT - 10)), [])
        with self.assertRaises(self.wire.FrameError):
            r.feed(b" " * 20)
        r = self.wire.Reader(authenticated=True)
        r.feed(b" " * (self.wire.PRE_AUTH_LIMIT * 2))  # a member may send more
        with self.assertRaises(self.wire.FrameError):
            r.feed(b" " * self.wire.FRAME_LIMIT)

    def test_a_complete_line_over_the_limit_is_refused(self):
        r = self.wire.Reader()
        line = self.wire.encode({"t": "join", "pad": "x" * self.wire.PRE_AUTH_LIMIT})
        with self.assertRaises(self.wire.FrameError):
            r.feed(line)
        self.assertEqual(len(self.wire.Reader(authenticated=True).feed(line)), 1)

    def test_an_unauthenticated_peer_gets_a_few_frames_only(self):
        """GUARD preauth-frames. Mutation: no frame count before authentication
        -> a joiner can keep a connection busy forever."""
        r = self.wire.Reader()
        ping = self.wire.encode({"t": "join"})
        self.assertEqual(len(r.feed(ping * self.wire.PRE_AUTH_FRAMES)), self.wire.PRE_AUTH_FRAMES)
        with self.assertRaises(self.wire.FrameError):
            r.feed(ping)
        r = self.wire.Reader()
        r.feed(ping)
        r.authenticate()
        self.assertEqual(len(r.feed(ping * 50)), 50)

    def test_a_frame_must_be_a_json_object_with_a_type(self):
        """GUARD frame-object. Mutation: any JSON value is a frame -> code that
        reads msg["t"] crashes on a peer's list or string."""
        for bad in (b"[1, 2]\n", b'"t"\n', b'{"t": 1}\n', b"{nope\n", b"\xff\xfe\n", b"{}\n"):
            with self.assertRaises(self.wire.FrameError, msg=bad):
                self.wire.Reader(authenticated=True).feed(bad)


if __name__ == "__main__":
    unittest.main()
