#!/usr/bin/env python3
"""One AIOS interaction, using only Python's standard library.

Run from any directory against an already-running AIOS server. This writes demo
identities and messages. See docs/integration.md for the API contract.
"""
import argparse
import hashlib
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"AIOS HTTP {status}: {body}")


class Client:
    def __init__(self, base_url, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, body=None, **query):
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise APIError(exc.code, exc.read().decode("utf-8", "replace")) from exc

    def ready_hud(self, instance_id, node_id, attempts=6):
        for attempt in range(attempts):
            # A 409 is a coordinate conflict: surface it to the caller rather
            # than silently changing the node or falling back to /frame.
            hud = self.request("POST", f"/instance/{instance_id}/hud",
                               through_node_id=node_id, wait_ms=2500)
            freshness = hud.get("freshness", {})
            if (hud.get("generation_ready") is True
                    and freshness.get("requested_source_node_id") == node_id
                    and freshness.get("source_current") is True
                    and not freshness.get("replayed_snapshot")):
                return hud
            if attempt + 1 < attempts:
                time.sleep(min(0.5 * 2 ** attempt, 4))
        raise RuntimeError(f"HUD not ready after {attempts} requests: {hud.get('freshness')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--conversation", default="developer-demo-001",
                        help="Keep stable to resume; change for a new conversation")
    parser.add_argument("--turn", default="1", help="Stable unique turn ID within the conversation")
    parser.add_argument("--message", default="The brass key is on the kitchen table.")
    parser.add_argument("--reply", help="Optional actual model response to record after preparing the HUD")
    args = parser.parse_args()
    client = Client(args.base_url)
    character, user, scope = "docs-guide", "docs-human", "conversation"
    session = client.request("POST", "/session", {
        "source": "developer-demo", "source_session_id": args.conversation,
        "topic": "AIOS developer tutorial",
    })["session_id"]
    for actor, name in ((character, "Guide"), (user, "Human")):
        client.request("POST", f"/character/{quote(actor, safe='')}/identity/bootstrap/card", {
            "card": {"name": name}, "source_name": "developer-demo",
            "replace_authored_facets": False, "auto_accept_authored": False,
        })
    activation = {"session_id": session, "user_name": user, "scope_key": scope}
    agent = client.request("POST", f"/character/{character}/activate", {
        **activation, "controller_type": "agent",
    })
    client.request("POST", f"/character/{user}/activate", {
        **activation, "world_id": agent["world_id"],
        "controller_type": "human", "controller_ref": user,
    })

    def ingest(text, role, speaker, recipient):
        # The same logical message and content get the same key on retry.
        # Different turns containing identical text must get different keys.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return client.request("POST", "/ingest", {
            "session_id": session, "character_id": character, "user_name": user,
            "scope_key": scope, "speaker_type": role, "speaker_id": speaker,
            "recipient_id": recipient, "kind": "chat_message", "text": text,
            "payload": {"source": "developer-demo"},
            "dedupe_key": f"developer-demo:{session}:{args.turn}:{role}:{digest}",
        })

    event = ingest(args.message, "user", user, character)
    print(json.dumps({"session_id": session, "instance_id": agent["instance_id"],
                      "world_id": agent["world_id"], "ingest": event}, indent=2))
    if not event.get("source_current", True):
        raise RuntimeError("This replay is behind the source head; resume the latest turn.")
    hud = client.ready_hud(agent["instance_id"], event["node_id"])
    print(hud["text"])
    # Your application calls its chosen LLM here, with hud['text'] plus its
    # instruction and conversation messages. Pass the actual result as --reply
    # to exercise recording, or replace this block with your provider call.
    if args.reply is not None:
        reply = ingest(args.reply, "character", character, user)
        print(json.dumps({"reply_ingest": reply}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (APIError, URLError, RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
