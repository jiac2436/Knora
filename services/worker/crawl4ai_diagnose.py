"""Probe Crawl4AI directly, without HTML/text fallback or database writes."""
from __future__ import annotations

import argparse
import json
import re
import time

import httpx

from app.config import settings
from .web_reader import (CRAWL4AI_READ_TIMEOUT_SECONDS, GENERAL_CLEANING_CONFIG, MAX_CRAWL_RESPONSE_BYTES,
                         _is_wechat_verification, _require_public_url)


def probe(url: str, cleaning: bool) -> dict:
    report = {"url": url, "cleaning": cleaning, "usable": False}
    started = time.monotonic()
    try:
        _require_public_url(url)
        with httpx.Client(timeout=httpx.Timeout(CRAWL4AI_READ_TIMEOUT_SECONDS, connect=5), trust_env=False) as client:
            with client.stream("POST", settings.crawl4ai_url.rstrip("/") + "/crawl",
                               headers={"Authorization": f"Bearer {settings.crawl4ai_token}"},
                               json={"urls": [url], "browser_config": {},
                                     "crawler_config": GENERAL_CLEANING_CONFIG if cleaning else {}}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRAWL_RESPONSE_BYTES:
                        raise ValueError("Crawl4AI response exceeds size limit")
        result = json.loads(body)["results"][0]
        markdown = result.get("markdown") or {}
        raw = (markdown.get("raw_markdown") or "") if isinstance(markdown, dict) else markdown
        fit = (markdown.get("fit_markdown") or "") if isinstance(markdown, dict) else ""
        selected = fit or raw
        title = (result.get("metadata") or {}).get("title") or ""
        parse_error = raw.lstrip().startswith("Crawl4AI Error:")
        error = re.search(r"Error Message:\s*([^\n]+)", raw) if parse_error else None
        challenge = _is_wechat_verification(url, title, selected)
        report.update(success=bool(result.get("success")), status_code=result.get("status_code"),
                      html_chars=len(result.get("html") or ""),
                      cleaned_html_chars=len(result.get("cleaned_html") or ""),
                      raw_markdown_chars=len(raw), fit_markdown_chars=len(fit),
                      challenge=challenge, parse_error=parse_error)
        report["usable"] = bool(result.get("success") and selected.strip()
                                and not report["parse_error"] and not challenge
                                and (result.get("status_code") or 200) < 400)
        if not result.get("success"):
            report["error"] = str(result.get("error_message") or "未提供")[:500]
        if error:
            report["error"] = error.group(1)[:180]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
        report["error"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            report["service_status_code"] = exc.response.status_code
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--cleaning", choices=("both", "on", "off"), default="both")
    args = parser.parse_args()
    if not settings.crawl4ai_url or not settings.crawl4ai_token:
        parser.error("Set PKA_CRAWL4AI_URL and PKA_CRAWL4AI_TOKEN")
    modes = [False, True] if args.cleaning == "both" else [args.cleaning == "on"]
    reports = []
    for url in args.urls:
        for mode in modes:
            report = probe(url, mode)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if all(item["usable"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
