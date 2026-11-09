"""Patch Crawl4AI 0.9.4's lxml attribute reading for malformed HTML.

dict(attrib) reads keys through __getitem__, which validates names parsed from
broken HTML. items() reads the parsed key/value pairs without that validation;
the existing whitelist then removes unwanted attributes as usual.
"""
from pathlib import Path

import crawl4ai.content_scraping_strategy as scraping

path = Path(scraping.__file__)
source = path.read_text()
original = "old_attribs = dict(el.attrib)"
replacement = "old_attribs = dict(el.attrib.items())"
if source.count(original) != 1:
    raise RuntimeError("Crawl4AI source changed: review the attribute patch before building")
path.write_text(source.replace(original, replacement))
print("Applied PKA Crawl4AI malformed-attribute patch")

# The pinning proxy runs on the API event loop. A synchronous DNS lookup for
# each browser connection blocks health checks, other crawls and deadlines.
path = Path("/app/egress_proxy.py")
source = path.read_text()
for original in (
    'pin = resolve_and_pin(f"https://{host}:{port_s}")',
    'pin = resolve_and_pin(f"http://{sp.hostname}:{port}")',
):
    replacement = original.replace("resolve_and_pin(", "await asyncio.to_thread(resolve_and_pin, ")
    if source.count(original) != 1:
        raise RuntimeError("Crawl4AI proxy changed: review the DNS patch before building")
    source = source.replace(original, replacement)
path.write_text(source)
print("Applied PKA Crawl4AI nonblocking proxy DNS patch")
