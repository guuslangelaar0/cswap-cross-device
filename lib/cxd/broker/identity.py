"""Agent identity and TLS for the broker transport (docs/design/broker.md).

Each agent has an EC P-256 key and a self-signed certificate made with the
system `openssl` (the standard library cannot generate keys). The certificate
has a unique subject and key identifiers, so several of them can sit in one
trust bundle. A member is identified by its node id, the SHA-256 of its
certificate's DER encoding:

  - TLS verifies a peer against the bundle of member certificates;
  - classify(), after the handshake, is the pin. Member certificates are CAs
    (LibreSSL 2.8 builds no chain to a self-signed certificate without
    keyCertSign), so a certificate *signed by* a member would pass TLS
    verification; only the node-id check refuses it.

Server contexts ask for a client certificate but do not require one
(CERT_OPTIONAL): members present theirs, a joining machine presents none and
may only attempt a join. TLS 1.2 is the floor (macOS's python3 has LibreSSL
2.8, without TLS 1.3); only ECDHE key exchange with AEAD ciphers.

A certificate's validity starts a day before it was made, so a peer whose
clock lags (common after a laptop sleeps) accepts a fresh one.
"""

import fcntl
import hashlib
import os
import ssl
import subprocess
import tempfile
import time

from ..common import atomic_write, ensure_dir

CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"
MEMBER, JOINER = "member", "joiner"
BACKDATE_SECONDS = 86400
_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
# `openssl ca -selfsign` (in OpenSSL and LibreSSL alike) is the portable way to
# choose the start of the validity period.
_CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = %s
[ca]
default_ca = cxd_ca
[cxd_ca]
database = index.txt
new_certs_dir = .
serial = serial
default_md = sha256
policy = cxd_policy
unique_subject = no
[cxd_policy]
commonName = supplied
[cxd]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyCertSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""


class IdentityError(Exception):
    """The agent identity, a certificate or the trust bundle is unusable."""


def node_id_of_der(der):
    return hashlib.sha256(der).hexdigest()


def node_id_of_pem(pem):
    """The node id of the one certificate in pem (IdentityError otherwise)."""
    if not isinstance(pem, str) or pem.count(_PEM_BEGIN) != 1:  # GUARD:one-cert
        raise IdentityError("expected exactly one PEM certificate")
    try:
        return node_id_of_der(ssl.PEM_cert_to_DER_cert(pem.strip() + "\n"))
    except ValueError as e:
        raise IdentityError("not a PEM certificate: %s" % e)


class Identity(object):
    def __init__(self, directory):
        self.dir = directory
        self.key = os.path.join(directory, "key.pem")
        self.cert = os.path.join(directory, "cert.pem")
        try:
            with open(self.cert, "r", encoding="ascii") as f:
                self.cert_pem = f.read()
            self.node_id = node_id_of_pem(self.cert_pem)
            # The key must be the certificate's (a crash between the two writes).
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(self.cert, self.key)  # GUARD:identity-pair
        except (OSError, ValueError, ssl.SSLError, IdentityError) as e:
            raise IdentityError("the agent identity is unusable (%s); remove key.pem and cert.pem "
                                "to make a new one, then join this machine again" % e)


def ensure_identity(directory, openssl="openssl"):
    """The identity in directory, created on first use."""
    ensure_dir(directory)
    key, cert = os.path.join(directory, "key.pem"), os.path.join(directory, "cert.pem")
    lock = os.open(os.path.join(directory, "identity.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # GUARD:identity-lock
        if not (os.path.exists(key) and os.path.exists(cert)):
            _create(directory, key, cert, openssl)
        return Identity(directory)
    finally:
        os.close(lock)


def _create(directory, key, cert, openssl):
    with tempfile.TemporaryDirectory(dir=directory) as tmp:  # mode 700
        with open(os.path.join(tmp, "req.cnf"), "w") as f:
            # The CN goes in the config: LibreSSL ignores -subj with prompt = no.
            f.write(_CNF % ("cxd-" + os.urandom(8).hex()))
        with open(os.path.join(tmp, "index.txt"), "w"):
            pass
        with open(os.path.join(tmp, "serial"), "w") as f:
            f.write("7" + os.urandom(15).hex()[1:] + "\n")  # positive, random
        start = time.strftime("%y%m%d%H%M%SZ", time.gmtime(time.time() - BACKDATE_SECONDS))
        _run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "key.pem"], tmp)
        _run([openssl, "req", "-new", "-key", "key.pem", "-out", "req.csr", "-config", "req.cnf"], tmp)
        _run([openssl, "ca", "-batch", "-selfsign", "-config", "req.cnf", "-extensions", "cxd",
              "-keyfile", "key.pem", "-in", "req.csr", "-out", "cert.pem", "-notext",
              "-startdate", start, "-days", "3650"], tmp)
        os.chmod(os.path.join(tmp, "key.pem"), 0o600)
        os.replace(os.path.join(tmp, "key.pem"), key)
        os.replace(os.path.join(tmp, "cert.pem"), cert)


def _run(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise IdentityError("%s: %s" % (cmd[0], e))
    if r.returncode != 0:
        raise IdentityError("%s %s failed: %s" % (cmd[0], cmd[1], " ".join(r.stderr.split())[:200]))


def write_trust(path, cert_pems):
    """The trust bundle: one PEM per member, mode 600, replaced atomically."""
    for pem in cert_pems:
        node_id_of_pem(pem)  # one certificate each
    atomic_write(path, "".join(p if p.endswith("\n") else p + "\n" for p in cert_pems).encode("ascii"),
                 fsync=True)
    return path


def _harden(ctx):
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # GUARD:tls-floor
    ctx.set_ciphers(CIPHERS)  # GUARD:tls-ciphers
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.options |= getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    return ctx


def _load_trust(ctx, trust):
    if not trust or not os.path.isfile(trust) or not os.path.getsize(trust):
        raise IdentityError("no trust bundle at %s" % trust)
    ctx.load_verify_locations(trust)


def server_context(identity, trust):
    """Members present pinned certificates; a joiner presents none."""
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    ctx.load_cert_chain(identity.cert, identity.key)
    _load_trust(ctx, trust)
    ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


def client_context(identity, trust):
    """A member dialing a member: both sides authenticate. The dialer still
    checks the node id it expected (a member could sign another certificate)."""
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    ctx.check_hostname = False  # the pinned certificate is the identity, not a host name
    _load_trust(ctx, trust)
    ctx.verify_mode = ssl.CERT_REQUIRED  # GUARD:client-verify
    ctx.load_cert_chain(identity.cert, identity.key)
    return ctx


def join_client_context():
    """A machine joining: it presents no certificate and cannot verify the
    member yet; the short code binds the certificate it saw (see Pairing)."""
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the join code, not TLS, authenticates the member
    return ctx


def peer_node_id(sock):
    """The node id of the certificate the peer presented, or None."""
    der = sock.getpeercert(binary_form=True)
    return node_id_of_der(der) if der else None


def classify(sock, member_ids):
    """What the peer on a server-side socket may do: MEMBER (it presented a
    member's certificate), JOINER (it presented none: it may only try to
    join) or None (refuse it: a certificate that TLS verified but that is no
    member's, such as one a member's key signed)."""
    peer = peer_node_id(sock)
    if peer is None:
        return JOINER
    return MEMBER if peer in member_ids else None  # GUARD:pin-node-id
