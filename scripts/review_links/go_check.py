"""Check one Go that nate pressed on the List page before the target session acts on it."""
from __future__ import annotations

import argparse
import json
import sys
from http.client import HTTPConnection
from typing import cast
from urllib.parse import urlencode

from scripts.lib import conf
from scripts.production import staff
from scripts.review_links import go_ledger, review_link


def server_address() -> tuple[str, int]:
    """Find the running review-links server."""
    return review_link.link_host(), conf.read("review_links").whole("server_port")


def ask_server(go_id: str, caller: str) -> go_ledger.GoCheckAnswer:
    """Send one GET; the server marks the Go used when it says yes."""
    host, port = server_address()
    connection = HTTPConnection(host, port, timeout=conf.read("review_links").whole("page_request_timeout_seconds"))
    try:
        connection.request("GET", go_ledger.GO_CHECK_PATH + "?" + urlencode({"id": go_id, "caller": caller}))
        response = connection.getresponse()
        payload = cast(object, json.loads(response.read()))
    finally:
        connection.close()
    fields = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
    ok, text = fields.get("ok"), fields.get("text", fields.get("reason"))
    if not isinstance(ok, bool) or not isinstance(text, str):
        return {"ok": False, "text": f"the server gave HTTP {response.status}."}
    return {"ok": ok, "text": text}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check one List page Go for this session.")
    _ = parser.add_argument("go_id")
    args = parser.parse_args(argv)
    go_id = cast(str, args.go_id)
    try:
        caller = staff.snapshot().by_current_session(staff.caller_identity())
    except (OSError, ValueError) as error:
        print(f"go refused: the staff record is not available ({error}).")
        return 1
    if caller is None:
        print("go refused: this session has no staff row.")
        return 1
    try:
        answer = ask_server(go_id, caller.id)
    except (OSError, ValueError) as error:
        print(f"go refused: the server is not available ({error}).")
        return 1
    if answer["ok"]:
        print(f"go ok: {answer['text']}")
        return 0
    print(f"go refused: {answer['text']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
