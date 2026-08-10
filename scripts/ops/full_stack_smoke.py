"""Read-only smoke test through the public Next.js BFF and Demo API.

The check deliberately exercises real HTTP routes. It never mutates the
corpus, creates a session, or sends a request to an external AI provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class FullStackSmokeError(RuntimeError):
    """A bounded, operator-safe smoke-test failure."""


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(dict(payload)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise FullStackSmokeError("an HTTP surface returned an unexpected status")
            parsed = json.load(response)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise FullStackSmokeError("an HTTP surface is unavailable or returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise FullStackSmokeError("an HTTP surface returned a non-object payload")
    return parsed


def _request_page(url: str) -> None:
    try:
        with urlopen(Request(url, method="GET"), timeout=15) as response:
            content_type = response.headers.get_content_type()
            sample = response.read(512)
    except (HTTPError, URLError, OSError) as exc:
        raise FullStackSmokeError("a frontend page is unavailable") from exc
    if response.status != 200 or content_type != "text/html" or not sample:
        raise FullStackSmokeError("a frontend page did not return HTML")


def verify(web_url: str) -> dict[str, int | str]:
    base = web_url.rstrip("/")
    for path in ("/", "/dashboard", "/evidence", "/knowledge"):
        _request_page(f"{base}{path}")

    health = _request_json(f"{base}/api/magicforge/health")
    if (
        health.get("profile") != "demo"
        or health.get("read_only") is not True
        or health.get("synthetic_corpus") is not True
        or health.get("glm_configured") is not False
    ):
        raise FullStackSmokeError("the BFF does not expose the offline Demo identity")

    stats = _request_json(f"{base}/api/magicforge/stats")
    search = _request_json(
        f"{base}/api/magicforge/knowledge/search?query=attention&limit=60"
    )
    evidence_cards = search.get("evidence_cards")
    nodes = search.get("nodes")
    relationships = search.get("relationships")
    if not isinstance(evidence_cards, list) or not evidence_cards:
        raise FullStackSmokeError("the Evidence Browser has no Demo records")
    if not isinstance(nodes, list) or not nodes:
        raise FullStackSmokeError("the Knowledge Explorer has no Demo nodes")
    if not isinstance(relationships, list) or not relationships:
        raise FullStackSmokeError("the Knowledge Explorer has no Demo relationships")

    evidence_id = evidence_cards[0].get("id")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise FullStackSmokeError("the Demo Evidence Card has no stable identity")
    evidence = _request_json(
        f"{base}/api/magicforge/evidence/{quote(evidence_id, safe='')}"
    )
    if evidence.get("id") != evidence_id:
        raise FullStackSmokeError("the Evidence detail route returned the wrong record")

    chat = _request_json(
        f"{base}/api/magicforge/chat",
        method="POST",
        payload={"question": "How is attention represented in this Demo?"},
    )
    if (
        not isinstance(chat.get("result"), str)
        or not chat.get("result")
        or not isinstance(chat.get("sources"), list)
        or not chat.get("sources")
    ):
        raise FullStackSmokeError("Magic Chat did not return a retrieved Demo answer")

    console = _request_json(f"{base}/api/magicforge/research/console")
    try:
        provider = console["runtime"]["intelligence_instrument"]["provider"]
    except (KeyError, TypeError) as exc:
        raise FullStackSmokeError("the Demo Research Console response is incomplete") from exc
    if provider != "Offline deterministic Demo":
        raise FullStackSmokeError("the Demo unexpectedly selected an external AI provider")

    points = stats.get("qdrant_points")
    if not isinstance(points, int) or points <= 0:
        raise FullStackSmokeError("the Dashboard reports no Demo points")
    return {
        "status": "passed",
        "pages": 4,
        "evidence_cards": len(evidence_cards),
        "nodes": len(nodes),
        "relationships": len(relationships),
        "qdrant_points": points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-url", default="http://127.0.0.1:3000")
    arguments = parser.parse_args()
    try:
        report = verify(arguments.web_url)
    except FullStackSmokeError as exc:
        print(f"full-stack smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
