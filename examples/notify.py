#!/usr/bin/env python3
"""Standard library client. Caller supplies gateway credentials, never Gotify credentials."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("title")
    parser.add_argument("message")
    parser.add_argument("--source", default="personal-script")
    parser.add_argument("--severity", choices=["info", "warning", "critical"], default="info")
    args = parser.parse_args()
    token = os.getenv("NOTIFY_API_TOKEN", "")
    if not token:
        print("Set NOTIFY_API_TOKEN first", file=sys.stderr)
        return 2
    body = json.dumps({"source": args.source, "title": args.title,
                       "message": args.message, "severity": args.severity}).encode()
    req = urllib.request.Request(
        os.getenv("NOTIFY_URL", "http://127.0.0.1:8090/v1/notify"),
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.load(response)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as exc:
        # Only the gateway's safe public response is displayed.
        print(f"Gateway HTTP {exc.code}: {exc.read(8192).decode(errors='replace')}", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError):
        print("Gateway could not be reached; delivery outcome may be unknown", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
