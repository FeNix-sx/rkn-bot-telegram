"""Minimal async 3x-ui API client."""
from __future__ import annotations
import copy
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
import httpx

LOGGER = logging.getLogger(__name__)

class XUIAPIError(RuntimeError):
    """Raised when 3x-ui API call fails."""
    pass

class XUIAPI:
    """Async client for 3x-ui panel API (session-based auth)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 10.0,
        retries: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )
        self._is_ready = False

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        LOGGER.info("xui.login.start")
        resp = await self._client.post(
            "/login",
            data={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise XUIAPIError(f"Auth failed: {payload.get('msg')}")
        self._is_ready = True
        LOGGER.info("xui.login.ok")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self._is_ready:
            await self.login()

        attempt = 0
        while True:
            try:
                resp = await self._client.request(method, path, json=json_body)
                if resp.status_code == 401 and attempt == 0:
                    LOGGER.warning("xui.session.expired, relogin...")
                    self._is_ready = False
                    await self.login()
                    attempt += 1
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise XUIAPIError("Unexpected API payload type.")
                if not payload.get("success"):
                    raise XUIAPIError(payload.get("msg") or "3x-ui call failed without details")
                return payload.get("obj")
            except httpx.HTTPStatusError as exc:
                raise XUIAPIError(f"3x-ui HTTP error {exc.response.status_code} on {path}: {exc.response.text}") from exc
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt >= self._retries:
                    raise XUIAPIError(f"3x-ui network error on {path}: {exc}") from exc
                attempt += 1
                LOGGER.warning("xui.request.retry", extra={"path": path, "attempt": attempt})
            except json.JSONDecodeError as exc:
                raise XUIAPIError(f"Invalid JSON from 3x-ui on {path}.") from exc

    async def get_inbounds(self) -> list[dict[str, Any]]:
        # ТВОЯ СБОРКА ТРЕБУЕТ /list
        resp_obj = await self._request("GET", "/panel/api/inbounds/list")
        if not isinstance(resp_obj, list):
            raise XUIAPIError("Unexpected inbounds payload format.")
        return [dict(item) for item in resp_obj]

    async def resolve_inbound_id(self, tag_or_remark: str = "vless_reality") -> int:
        target = tag_or_remark.strip().lower()
        inbounds = await self.get_inbounds()
        matches = [
            ib for ib in inbounds
            if target in {str(ib.get("tag") or "").strip().lower(), str(ib.get("remark") or "").strip().lower()}
        ]
        if not matches: raise XUIAPIError(f"Inbound '{tag_or_remark}' not found.")
        if len(matches) > 1: raise XUIAPIError(f"Inbound '{tag_or_remark}' is ambiguous.")
        inbound_id = matches[0].get("id")
        if not isinstance(inbound_id, int): raise XUIAPIError("Inbound id type error.")
        return inbound_id

    async def add_client(self, inbound_id: int, email: str, uuid: str, limit_ip: int = 0, total_gb: int = 0, expiry_ms: int = 0) -> None:
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [{"id": uuid, "email": email, "limitIp": limit_ip, "enable": True, "totalGB": total_gb, "expiryTime": expiry_ms}]}),
        }
        await self._request("POST", "/panel/api/inbounds/addClient", json_body=payload)

    async def find_inbound_id_for_email(self, email: str) -> int:
        target = (email or "").strip()
        if not target:
            raise XUIAPIError("Empty client email.")
        for ib in await self.get_inbounds():
            iid = ib.get("id")
            if not isinstance(iid, int):
                continue
            s_raw = ib.get("settings")
            clients: list[Any] = []
            if isinstance(s_raw, str):
                try:
                    clients = json.loads(s_raw).get("clients", [])
                except json.JSONDecodeError:
                    continue
            elif isinstance(s_raw, dict):
                clients = s_raw.get("clients", [])
            else:
                continue
            if any(str(c.get("email") or "").strip() == target for c in clients):
                return iid
        raise XUIAPIError(f"Client email '{target}' not found in any inbound.")

    async def set_client_expiry(self, inbound_id: int, email: str, expiry_ms: int) -> None:
        settings = await self._get_inbound_settings(inbound_id)
        clients = settings.get("clients", [])
        target = (email or "").strip()
        client_uuid: str | None = None
        for c in clients:
            if str(c.get("email") or "").strip() == target:
                c["expiryTime"] = int(expiry_ms)
                raw_id = c.get("id")
                client_uuid = str(raw_id).strip() if raw_id is not None else None
                break
        else:
            raise XUIAPIError(f"Client '{target}' not found in inbound {inbound_id}.")
        if not client_uuid:
            raise XUIAPIError(f"Client '{target}' has no id (UUID) in inbound settings.")
        # 3x-ui UpdateInboundClient ожидает в settings.clients ровно ОДНОГО клиента;
        # иначе clients[0] не совпадает с обновляемым и срабатывает Duplicate email.
        one = copy.deepcopy(next(c for c in clients if str(c.get("email") or "").strip() == target))
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [one]})}
        path = f"/panel/api/inbounds/updateClient/{client_uuid}"
        await self._request("POST", path, json_body=payload)

    async def set_client_limit_ip(self, inbound_id: int, email: str, limit_ip: int) -> None:
        settings = await self._get_inbound_settings(inbound_id)
        clients = settings.get("clients", [])
        target = (email or "").strip()
        client_uuid: str | None = None
        for c in clients:
            if str(c.get("email") or "").strip() == target:
                c["limitIp"] = int(limit_ip)
                raw_id = c.get("id")
                client_uuid = str(raw_id).strip() if raw_id is not None else None
                break
        else:
            raise XUIAPIError(f"Client '{target}' not found in inbound {inbound_id}.")
        if not client_uuid:
            raise XUIAPIError(f"Client '{target}' has no id (UUID) in inbound settings.")
        one = copy.deepcopy(next(c for c in clients if str(c.get("email") or "").strip() == target))
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [one]})}
        path = f"/panel/api/inbounds/updateClient/{client_uuid}"
        await self._request("POST", path, json_body=payload)

    async def disable_client(self, inbound_id: int, email: str) -> None:
        settings = await self._get_inbound_settings(inbound_id)
        clients = settings.get("clients", [])
        target = (email or "").strip()
        client_uuid: str | None = None
        for c in clients:
            if str(c.get("email") or "").strip() == target:
                c["enable"] = False
                raw_id = c.get("id")
                client_uuid = str(raw_id).strip() if raw_id is not None else None
                break
        else:
            raise XUIAPIError(f"Client '{target}' not found in inbound {inbound_id}.")
        if not client_uuid:
            raise XUIAPIError(f"Client '{target}' has no id (UUID) in inbound settings.")
        one = copy.deepcopy(next(c for c in clients if str(c.get("email") or "").strip() == target))
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [one]})}
        path = f"/panel/api/inbounds/updateClient/{client_uuid}"
        await self._request("POST", path, json_body=payload)

    async def _get_inbound_settings(self, inbound_id: int) -> dict:
        for inbound in await self.get_inbounds():
            if inbound.get("id") == inbound_id:
                settings = inbound.get("settings")
                if isinstance(settings, str): return json.loads(settings)
                return settings or {}
        raise XUIAPIError(f"Inbound {inbound_id} not found.")

    async def build_or_get_subscription_url(self, *, inbound_id: int, email: str) -> str:
        settings = await self._get_inbound_settings(inbound_id)
        clients = settings.get("clients", [])
        sub_id = None
        for c in clients:
            if c.get("email") == email:
                sub_id = str(c.get("subId") or "").strip()
                break
        if not sub_id: raise XUIAPIError("subId not found.")
        parsed = urlparse(self._base_url)
        return f"{parsed.scheme}://{parsed.netloc}/sub/{sub_id}"