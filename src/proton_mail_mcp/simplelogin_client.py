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
