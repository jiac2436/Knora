"""Verify slow DNS cannot block API timers; blocked destinations stay blocked."""
import asyncio
import sys
import time

sys.path.insert(0, "/app")
import egress_proxy
from egress_broker import EgressBlocked, PinnedTarget


async def check(method, blocked):
    def resolve(url):
        time.sleep(0.4)
        if blocked:
            raise EgressBlocked()
        return PinnedTarget("https", "example.com", 443, "93.184.216.34")

    egress_proxy.resolve_and_pin = resolve
    proxy = egress_proxy.PinningProxy()
    dialed = []

    async def dial(pin, port):
        dialed.append(pin.ip)
        raise ConnectionError("test does not dial external hosts")

    proxy._dial = dial
    await proxy.start()
    reader, writer = await asyncio.open_connection(proxy.bound_host, proxy.bound_port)
    request = (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n" if method == "CONNECT"
               else b"GET http://example.com/ HTTP/1.1\r\n\r\n")
    writer.write(request)
    await writer.drain()
    started = time.monotonic()
    await asyncio.sleep(0.05)
    assert time.monotonic() - started < 0.25, "DNS blocked the API event loop"
    if method == "CONNECT" or blocked:
        response = await asyncio.wait_for(reader.read(1024), timeout=2)
        assert b"403 Forbidden" in response
    if blocked:
        assert not dialed, "Blocked destination was dialed"
    writer.close()
    await writer.wait_closed()
    await proxy.stop()


async def main():
    for method in ("CONNECT", "GET"):
        await check(method, blocked=True)
    await check("CONNECT", blocked=False)


asyncio.run(main())
print("Crawl4AI proxy DNS responsiveness and blocking regression checks passed")
