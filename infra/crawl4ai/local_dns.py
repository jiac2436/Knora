"""Optional local proxy DNS bridge; returns real addresses via HTTPS DNS."""
import os
import pwd
import socketserver

import requests


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        query, sock = self.request
        if len(query) < 12 or len(query) > 4096:
            return
        try:
            response = requests.post(
                "https://cloudflare-dns.com/dns-query", data=query,
                headers={"Content-Type": "application/dns-message", "Accept": "application/dns-message"},
                proxies={"https": os.environ["PKA_CRAWL4AI_DOH_PROXY"]}, timeout=8)
            response.raise_for_status()
            if response.content[:2] == query[:2]:
                sock.sendto(response.content, self.client_address)
        except requests.RequestException:
            sock.sendto(query[:2] + b"\x81\x82" + query[4:], self.client_address)


class Server(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


server = Server(("127.0.0.1", 53), DNSHandler)
user = pwd.getpwnam("appuser")
os.initgroups(user.pw_name, user.pw_gid)
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
server.serve_forever()
