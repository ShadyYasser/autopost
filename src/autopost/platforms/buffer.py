"""Buffer GraphQL API client (https://api.buffer.com, Bearer API key).

Phase 3 uses it only for read-only checks; publishing through Buffer arrives in phase 4.
"""

from __future__ import annotations

import requests

API = "https://api.buffer.com"


def channel_matches(channel: dict, wanted: str) -> bool:
    """Configured channel name vs Buffer's name/displayName (case and extra spaces ignored)."""
    norm = lambda v: " ".join(str(v or "").split()).casefold()  # noqa: E731
    return norm(wanted) in {norm(channel.get("name")), norm(channel.get("displayName"))}


class BufferError(Exception):
    pass


class BufferClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, on_request=None):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.on_request = on_request          # callback counting requests against the account budget

    def query(self, query: str, variables: dict | None = None) -> dict:
        if self.on_request:
            self.on_request()
        try:
            resp = self.session.post(API, json={"query": query, "variables": variables or {}}, timeout=60,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        except requests.RequestException as exc:
            raise BufferError(f"network error: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise BufferError(f"rate limited (retry after {resp.headers.get('Retry-After', '?')}s)")
        try:
            body = resp.json()
        except ValueError:
            raise BufferError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
        if resp.status_code >= 400 or body.get("errors"):
            msgs = "; ".join(e.get("message", "") for e in body.get("errors") or []) or resp.text[:200]
            raise BufferError(f"HTTP {resp.status_code}: {msgs}")
        return body.get("data") or {}

    def account(self) -> dict:
        return self.query("query { account { email organizations { id name } } }")["account"]

    def channels(self, organization_id: str) -> list[dict]:
        return self.query(
            "query($input: ChannelsInput!) { channels(input: $input) "
            "{ id name displayName service isDisconnected isLocked } }",
            {"input": {"organizationId": organization_id}})["channels"]
