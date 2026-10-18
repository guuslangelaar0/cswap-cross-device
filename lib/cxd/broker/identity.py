"""Agent identity and TLS for the broker transport (docs/design/broker.md).

Each agent has an EC P-256 key and a self-signed certificate made with the
system `openssl` (the standard library cannot generate keys). The certificate
has a unique subject and key identifiers, so several of them can sit in one
trust bundle. A member is identified by its node id, the SHA-256 of its
certificate's DER encoding:

  - TLS verifies a peer against the bundle of member certificates;
  - the node-id check after the handshake is the pin. Member certificates are
    CAs, so a certificate *signed by* a member would pass TLS verification;
    only the node-id check refuses it.

Server contexts ask for a client certificate but do not require one
(CERT_OPTIONAL): members present theirs, a joining machine presents none and
may only attempt a join. TLS 1.2 is the floor (macOS's python3 has LibreSSL
2.8, without TLS 1.3); only ECDHE key exchange with AEAD ciphers.
"""

import hashlib
import os
import ssl
import subprocess
import tempfile

from ..common import ensure_dir

CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"
_REQ_CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = %s
[cxd]
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyCertSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


class IdentityError(Exception):
    """The agent identity could not be created or read."""


def node_id_of_der(der):
    return hashlib.sha256(der).hexdigest()


def node_id_of_pem(pem):
    return node_id_of_der(ssl.PEM_cert_to_DER_cert(pem))


class Identity(object):
    def __init__(self, directory):
        self.dir = directory
        self.key = os.path.join(directory, "key.pem")
        self.cert = os.path.join(directory, "cert.pem")
        with open(self.cert, "r", encoding="ascii") as f:
            self.cert_pem = f.read()
        self.node_id = node_id_of_pem(self.cert_pem)


def ensure_identity(directory, openssl="openssl"):
    """The identity in directory, created on first use."""
    ensure_dir(directory)
    key, cert = os.path.join(directory, "key.pem"), os.path.join(directory, "cert.pem")
    if os.path.exists(key) and os.path.exists(cert):
        return Identity(directory)
    old = os.umask(0o077)  # the key is created mode 600
    try:
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            cnf = os.path.join(tmp, "req.cnf")
            with open(cnf, "w") as f:
                # The CN goes in the config: LibreSSL ignores -subj with prompt = no.
                f.write(_REQ_CNF % ("cxd-" + os.urandom(8).hex()))
            tkey, tcert = os.path.join(tmp, "key.pem"), os.path.join(tmp, "cert.pem")
            _run([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", tkey])
            _run([openssl, "req", "-new", "-x509", "-key", tkey, "-out", tcert, "-days", "3650",
                  "-config", cnf, "-extensions", "cxd"])
            os.replace(tkey, key)
            os.replace(tcert, cert)
    finally:
        os.umask(old)
    os.chmod(key, 0o600)
    return Identity(directory)


def _run(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise IdentityError("%s: %s" % (cmd[0], e))
    if r.returncode != 0:
        raise IdentityError("%s %s failed: %s" % (cmd[0], cmd[1], " ".join(r.stderr.split())[:200]))


def write_trust(path, cert_pems):
    """The trust bundle: one PEM per member, mode 600, replaced atomically."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "".join(p if p.endswith("\n") else p + "\n" for p in cert_pems).encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def _harden(ctx):
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.options |= getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    return ctx


def server_context(identity, trust):
    """Members present pinned certificates; a joiner presents none."""
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    ctx.load_cert_chain(identity.cert, identity.key)
    if trust and os.path.exists(trust) and os.path.getsize(trust):
        ctx.load_verify_locations(trust)
    ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


def client_context(identity, trust):
    """A member dialing a member: both sides authenticate."""
    ctx = _harden(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    ctx.check_hostname = False  # the pinned certificate is the identity, not a host name
    ctx.load_verify_locations(trust)
    ctx.verify_mode = ssl.CERT_REQUIRED
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


def is_member(peer_id, member_ids):
    """The pin: TLS verification alone would also accept a certificate that a
    member's (CA) certificate signed."""
    return peer_id is not None and peer_id in member_ids  # GUARD:pin-node-id
