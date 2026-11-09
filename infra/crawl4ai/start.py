"""Set up optional local DNS, then run Crawl4AI as its original appuser."""
import os
import pwd
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

proxy = os.environ.get("PKA_CRAWL4AI_DOH_PROXY")
if proxy:
    parts = urlsplit(proxy)
    if parts.scheme != "http" or not parts.hostname or parts.username or parts.password:
        raise ValueError("PKA_CRAWL4AI_DOH_PROXY requires an HTTP proxy without credentials")
    # Resolve the local proxy before switching DNS; avoid resolving it through DoH.
    address = socket.gethostbyname(parts.hostname)
    os.environ["PKA_CRAWL4AI_DOH_PROXY"] = parts._replace(
        netloc=f"{address}:{parts.port or 80}").geturl()
    process = subprocess.Popen([sys.executable, "/opt/pka-crawl4ai/local_dns.py"])
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError("Local Crawl4AI DNS bridge failed to start")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.bind(("127.0.0.1", 53))
            except OSError:
                break
        time.sleep(0.1)
    else:
        raise RuntimeError("Local Crawl4AI DNS bridge did not bind port 53")
    Path("/etc/resolv.conf").write_text("nameserver 127.0.0.1\noptions timeout:10 attempts:2\n")
    print("PKA Crawl4AI: local proxy DNS enabled; internal URL protection remains enabled", flush=True)

user = pwd.getpwnam("appuser")
# Docker created these pipes while the bootstrap ran as root. Supervisord
# reopens /dev/stdout and /dev/stderr after dropping privileges.
for descriptor in (0, 1, 2):
    os.fchown(descriptor, user.pw_uid, user.pw_gid)
os.initgroups(user.pw_name, user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])
