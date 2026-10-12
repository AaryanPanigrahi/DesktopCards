"""One place for "did this request really come from this machine?".

Both servers bind 127.0.0.1, and that is what keeps them off the network:
the kernel refuses a connection to a loopback socket from any other host,
so nothing on the LAN and nothing behind a public IP can reach them, with
or without a firewall rule. That part was never the hole.

What loopback cannot stop is the browser already running here, and two
attacks follow from it. Both arrive from a page someone merely visited, with
no code running on this machine:

  DNS rebinding   attacker.com is published with a very short TTL, then
                  re-resolved to 127.0.0.1. The attacker's own JavaScript
                  fetches http://attacker.com:5000/data.json, which the
                  browser now considers same-origin, so it can read the
                  reply -- every balance, bank name and last-four. Loopback
                  is no defence because the connection genuinely is local.
                  The giveaway is the Host header, which still says
                  attacker.com.

  CSRF            a cross-origin page POSTs to /refresh, /restart or
                  /api/quit. It cannot read the response, but it does not
                  need to in order to force a Plaid pull or kill a server.

Hence two rules: the Host must name this machine, and anything the browser
itself labels cross-site is refused.

Deliberately NOT covered: another process running on this machine. A
request with neither an Origin nor a Sec-Fetch-Site header is allowed
through, because that's an ordinary non-browser client -- this app's own
urllib calls among them -- and a hostile local process is not a threat this
layer can do anything about. It could read config.json off the disk instead,
so a check on the HTTP layer would be theatre. The attacks above are
different in kind: they need nothing more than a tab being open.
"""

import ipaddress

# Not a single literal. Whatever spelling the browser happens to use for
# this machine has to pass: "localhost" may resolve to ::1 rather than
# 127.0.0.1 depending on the OS and the client, Chromium and Python's
# urllib don't always agree on which, and the whole 127.0.0.0/8 block is
# loopback, not just .1. So anything that parses as a loopback IP is
# accepted and the name "localhost" is accepted alongside it, rather than
# matching one hardcoded address and breaking the moment something uses
# another valid way of saying the same thing.
LOCAL_HOSTNAMES = frozenset({"localhost"})

# same-origin is our own page talking to its own server. "none" is a
# user-initiated load: typing the address, or webbrowser.open.
SAFE_FETCH_SITES = frozenset({"same-origin", "none"})

SAFE_METHODS = frozenset({"GET", "HEAD"})


def _authority(value):
    """The host[:port] part of a Host header or an Origin URL."""
    text = (value or "").strip()
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0]


def _split_authority(authority):
    """(hostname, port) with the port as a string, or None if absent."""
    if authority.startswith("["):          # [::1]:5000
        host, _, rest = authority.partition("]")
        return (host + "]").lower(), (rest.lstrip(":") or None)
    # A bare IPv6 literal: "::1", "0:0:0:0:0:0:0:1". Brackets are required
    # when a port follows, so more than one colon and no brackets can only
    # be an address -- splitting on the first colon would otherwise leave
    # the host empty and refuse a request from this very machine.
    if authority.count(":") > 1:
        return authority.lower(), None
    host, _, port = authority.partition(":")
    return host.lower(), (port or None)


def is_loopback_name(host):
    """Whether this hostname is a way of saying "this machine".

    Any loopback IP in either family, plus the name localhost. Parsed
    rather than compared against a list, so 127.0.0.1, 127.0.0.2, ::1 and
    0:0:0:0:0:0:0:1 are all recognised for what they are."""
    if host in LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False  # a real hostname, so not this machine


def names_this_machine(value, port):
    """Whether a Host/Origin value points at this machine on our port."""
    host, got_port = _split_authority(_authority(value))
    if not is_loopback_name(host):
        return False
    # A mismatched port is some other local service's name being used on
    # ours, which is not something any page of ours ever does. The port is
    # the one the server actually bound, never a hardcoded number.
    return got_port is None or got_port == str(port)


def rejection(port, method="GET", host=None, origin=None,
              fetch_site=None, fetch_mode=None):
    """Why this request should be refused, or None to allow it.

    Returns a sentence suitable for a log line. The caller answers 403."""
    # A missing Host is allowed: HTTP/1.1 requires one and every client here
    # sends it, but more to the point a rebinding attack necessarily carries
    # the attacker's own domain, so refusing a request for lacking a header
    # that no attack omits would cost compatibility and buy nothing.
    if host and not names_this_machine(host, port):
        return f"Host header {host!r} does not name this machine"

    site = (fetch_site or "").lower()
    if site and site not in SAFE_FETCH_SITES:
        # One exemption: arriving by a link or a redirect. A cross-site GET
        # navigation is how an OAuth hand-back from a bank returns, and how
        # clicking a localhost link in another app gets here; the response
        # becomes a page at our own origin rather than data handed to
        # someone else's script. Cross-site POSTs and every cross-site
        # fetch/XHR/subresource still get refused.
        navigating = (fetch_mode or "").lower() == "navigate"
        if not (navigating and (method or "").upper() in SAFE_METHODS):
            return f"cross-site request (Sec-Fetch-Site: {fetch_site})"

    # For browsers too old to send Sec-Fetch-*. "null" is a sandboxed or
    # opaque origin, which nothing of ours produces.
    if origin and not names_this_machine(origin, port):
        return f"Origin {origin!r} does not name this machine"

    return None
