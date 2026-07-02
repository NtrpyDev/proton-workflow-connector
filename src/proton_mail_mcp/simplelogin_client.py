from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .redaction import redact_mapping

Requester = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None, Mapping[str, Any] | None], Any]


@dataclass
class SimpleLoginError(RuntimeError):
    status_code: int
    message: str

    def __str__(self) -> str:
        return f"SimpleLogin API error {self.status_code}: {self.message}"


class SimpleLoginClient:
    def __init__(self, settings: Settings, *, requester: Requester | None = None) -> None:
        self.settings = settings
        self._requester = requester

    def user_info(self) -> dict[str, Any]:
        return self._request("GET", "/api/user_info")

    def stats(self) -> dict[str, Any]:
        return self._request("GET", "/api/stats")

    def list_aliases(
        self,
        *,
        page_id: int = 0,
        pinned: bool | None = None,
        disabled: bool | None = None,
        enabled: bool | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        filters = [pinned is not None, disabled is not None, enabled is not None]
        if sum(filters) > 1:
            raise ValueError("pinned, disabled, and enabled filters are mutually exclusive")
        params: dict[str, Any] = {"page_id": page_id}
        if pinned is not None:
            params["pinned"] = str(pinned).lower()
        if disabled is not None:
            params["disabled"] = str(disabled).lower()
        if enabled is not None:
            params["enabled"] = str(enabled).lower()
        if query:
            return self._request("POST", "/api/v2/aliases", params=params, json_body={"query": query})
        return self._request("GET", "/api/v2/aliases", params=params)

    def get_alias(self, alias_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/aliases/{alias_id}")

    def poll_aliases(
        self,
        *,
        last_id: int = 0,
        query: str | None = None,
        limit: int = 50,
        max_pages: int = 20,
    ) -> dict[str, Any]:
        """Return aliases created since a stored cursor, for trigger and webhook workflows.

        The cursor is the maximum alias id seen so far. SimpleLogin has no push API, so this mirrors
        the IMAP :meth:`BridgeMailClient.poll_folder` cursor engine: on the first poll (no cursor)
        this baselines to the current highest alias id and returns nothing, so consumers are never
        flooded with the full alias list. Alias ids are monotonic, so a newly created alias always
        has an id greater than the cursor.

        SimpleLogin does **not** sort the alias list purely by id — pinned aliases and recent
        activity float to the top, so a low id can appear on page 0 ahead of much newer aliases.
        We therefore cannot stop paging as soon as a page dips below the cursor (that would miss new
        aliases sitting on later pages). Instead we read pages until an empty page or the ``max_pages``
        safety cap, then filter by id. Newly created aliases have the highest ids and land on the
        first pages, so the cap never hides recent activity. ``query`` matches a substring of the
        alias email. If more new aliases exist than ``limit``, the extra ones roll to the next poll.
        """
        if limit < 0:
            raise ValueError("limit must be zero or greater")
        collected: dict[int, dict[str, Any]] = {}
        truncated = False
        for page in range(max_pages):
            page_items = _alias_page(self.list_aliases(page_id=page))
            if not page_items:
                break
            for item in page_items:
                collected[int(item["id"])] = item
        else:
            truncated = True  # hit the page cap without reaching the end of the list
        head_id = max(collected, default=last_id)
        if last_id == 0:
            return {"aliases": [], "cursor_id": head_id, "baseline": True, "more": False}

        newer = (item for item in collected.values() if int(item["id"]) > last_id)
        fresh = sorted(newer, key=lambda item: int(item["id"]))
        if query:
            needle = query.casefold()
            fresh = [item for item in fresh if needle in str(item.get("email", "")).casefold()]
        delivered = fresh[:limit] if limit else fresh
        more = len(fresh) > len(delivered) or truncated
        cursor_id = int(delivered[-1]["id"]) if more and delivered else head_id
        return {"aliases": delivered, "cursor_id": cursor_id, "baseline": False, "more": more}

    def create_random_alias(
        self,
        *,
        hostname: str | None = None,
        mode: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if mode is not None and mode not in {"uuid", "word"}:
            raise ValueError("mode must be either 'uuid' or 'word'")
        params = _optional_params(hostname=hostname, mode=mode)
        body = _compact({"note": note})
        return self._request("POST", "/api/alias/random/new", params=params, json_body=body or {})

    def get_alias_options(self, *, hostname: str | None = None) -> dict[str, Any]:
        """Fetch the suffixes available for a custom alias, including the signed_suffix
        create_custom_alias requires. Suffix signatures expire after a few minutes, so fetch
        options immediately before creating."""
        return self._request("GET", "/api/v5/alias/options", params=_optional_params(hostname=hostname))

    def create_custom_alias(
        self,
        *,
        alias_prefix: str,
        signed_suffix: str,
        mailbox_ids: Sequence[int],
        hostname: str | None = None,
        note: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        if not mailbox_ids:
            raise ValueError("mailbox_ids must contain at least one mailbox id")
        body = _compact(
            {
                "alias_prefix": alias_prefix,
                "signed_suffix": signed_suffix,
                "mailbox_ids": list(mailbox_ids),
                "note": note,
                "name": name,
            }
        )
        return self._request(
            "POST", "/api/v3/alias/custom/new", params=_optional_params(hostname=hostname), json_body=body
        )

    def update_alias(
        self,
        alias_id: int,
        *,
        note: str | None = None,
        mailbox_id: int | None = None,
        mailbox_ids: Sequence[int] | None = None,
        name: str | None = None,
        disable_pgp: bool | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        body = _compact(
            {
                "note": note,
                "mailbox_id": mailbox_id,
                "mailbox_ids": list(mailbox_ids) if mailbox_ids is not None else None,
                "name": name,
                "disable_pgp": disable_pgp,
                "pinned": pinned,
            }
        )
        if not body:
            raise ValueError("update_alias requires at least one field to update")
        return self._request("PATCH", f"/api/aliases/{alias_id}", json_body=body)

    def toggle_alias(self, alias_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/aliases/{alias_id}/toggle", json_body={})

    def delete_alias(self, alias_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/api/aliases/{alias_id}")

    def list_alias_contacts(self, alias_id: int, *, page_id: int = 0) -> dict[str, Any]:
        return self._request("GET", f"/api/aliases/{alias_id}/contacts", params={"page_id": page_id})

    def create_alias_contact(self, alias_id: int, *, contact: str) -> dict[str, Any]:
        return self._request("POST", f"/api/aliases/{alias_id}/contacts", json_body={"contact": contact})

    def list_mailboxes(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/mailboxes")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.settings.require_simplelogin()
        url = f"{self.settings.simplelogin_base_url}{path}"
        headers = {
            "Authentication": self.settings.simplelogin_api_key,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        if self._requester is not None:
            return self._normalize_response(self._requester(method, url, headers, params, json_body))

        import httpx

        with httpx.Client(timeout=self.settings.request_timeout) as client:
            response = client.request(method, url, headers=headers, params=params, json=json_body)
        return self._normalize_httpx_response(response)

    def _normalize_httpx_response(self, response: Any) -> dict[str, Any]:
        if response.status_code >= 400:
            raise SimpleLoginError(response.status_code, _response_error(response))
        if not response.content:
            return {"ok": True}
        data = response.json()
        return redact_mapping(data) if isinstance(data, dict) else {"data": data}

    def _normalize_response(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            status_code = int(response.get("status_code", 200))
            if status_code >= 400:
                message = str(response.get("error") or response.get("message") or response)
                raise SimpleLoginError(status_code, message)
            data = response.get("json", response)
            return redact_mapping(data) if isinstance(data, dict) else {"data": data}
        return {"data": response}


def _alias_page(response: Any) -> list[dict[str, Any]]:
    aliases = response.get("aliases") if isinstance(response, Mapping) else None
    return [item for item in (aliases or []) if isinstance(item, Mapping) and item.get("id") is not None]


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _optional_params(**values: Any) -> dict[str, Any]:
    return _compact(values)


def _response_error(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        return str(response.text)
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)
    return str(data)
