"""Framing for broker links and the agent's local socket (docs/design/broker.md,
Protocol): one JSON object per line, UTF-8, ending with a newline, with a
type `t`. The size limit holds before a newline arrives, so a peer cannot
fill memory with one endless line. Before a link is authenticated (a joining
machine) the limit is small and only a few frames are allowed.
"""

import json

PRE_AUTH_LIMIT = 64 * 1024
PRE_AUTH_FRAMES = 4
FRAME_LIMIT = 8 * 1024 * 1024
MAX_INT_DIGITS = 4300  # as Python 3.11+ limits int(); 3.9 would parse any length in quadratic time


class FrameError(Exception):
    """The peer broke the framing rules; the link must be closed."""


def _is_frame(msg):
    return isinstance(msg, dict) and isinstance(msg.get("t"), str)


def _no_constant(name):
    raise ValueError("%s is not JSON" % name)


def _bounded_int(text):
    if len(text) > MAX_INT_DIGITS:
        raise ValueError("an integer of %d digits" % len(text))
    return int(text)


_decoder = json.JSONDecoder(parse_constant=_no_constant, parse_int=_bounded_int)


def encode(msg):
    """One frame, as bytes. FrameError for a message that is not a JSON object
    with a string `t`, or that would exceed FRAME_LIMIT."""
    if not _is_frame(msg):
        raise FrameError("a frame is a JSON object with a string 't'")
    try:
        data = (json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError) as e:  # NaN, a lone surrogate, too deep...
        raise FrameError("not a frame: %s" % e)
    if len(data) > FRAME_LIMIT:
        raise FrameError("frame of %d bytes exceeds %d" % (len(data), FRAME_LIMIT))
    return data


class Reader(object):
    """Splits a byte stream into frames and enforces the limits."""

    def __init__(self, authenticated=False):
        """A link is classified at its TLS handshake (member or joiner), so a
        member's reader starts authenticated; authenticate() only takes
        effect for bytes fed after it."""
        self.buf = bytearray()
        self.scanned = 0  # the buffer has no newline before this offset
        self.authenticated = authenticated
        self.frames = 0

    def authenticate(self):
        self.authenticated = True

    @property
    def limit(self):
        return FRAME_LIMIT if self.authenticated else PRE_AUTH_LIMIT

    def feed(self, data):
        """Add received bytes; return the complete messages they finish."""
        self.buf += data
        out = []
        while True:
            nl = self.buf.find(b"\n", self.scanned)
            if nl < 0:
                self.scanned = len(self.buf)
                if len(self.buf) > self.limit:  # GUARD:frame-limit
                    raise FrameError("frame exceeds %d bytes" % self.limit)
                return out
            if nl + 1 > self.limit:
                raise FrameError("frame exceeds %d bytes" % self.limit)
            line = bytes(self.buf[:nl])
            del self.buf[: nl + 1]
            self.scanned = 0
            self.frames += 1
            if not self.authenticated and self.frames > PRE_AUTH_FRAMES:  # GUARD:preauth-frames
                raise FrameError("too many frames before authentication")
            try:
                msg = _decoder.decode(line.decode("utf-8"))
            except (ValueError, RecursionError):  # GUARD:frame-decode (UnicodeDecodeError is a ValueError)
                raise FrameError("frame is not UTF-8 JSON (or nests too deep, or holds NaN or a huge number)")
            if not _is_frame(msg):  # GUARD:frame-object
                raise FrameError("frame is not a JSON object with a string 't'")
            out.append(msg)
