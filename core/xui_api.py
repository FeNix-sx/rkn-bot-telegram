"""Minimal async 3x-ui API client."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

LOGGER = logging.getLogger(__name__)

class XUIAPIError(RuntimeError):
    """Raised when 3x-ui API call fails."""

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

    async def __aenter__(self) -> XUIAPI:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

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

                # Авто-релогин при протухшей сессии
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
                    raise XUIAPIError(
                        payload.get("msg") or "3x-ui call failed without details"
                    )
                return payload.get("obj")

            except httpx.HTTPStatusError as exc:
                raise XUIAPIError(
                    f"3x-ui HTTP error {exc.response.status_code} on {path}: {exc.response.text}"
                ) from exc
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt >= self._retries:
                    raise XUIAPIError(f"3x-ui network error on {path}: {exc}") from exc
                attempt += 1
                LOGGER.warning("xui.request.retry", extra={"path": path, "attempt": attempt})
            except json.JSONDecodeError as exc:
                raise XUIAPIError(f"Invalid JSON from 3x-ui on {path}.") from exc

    async def get_inbounds(self) -> list[dict[str, Any]]:
        # Без /list. Стандартный эндпоинт.
        resp_obj = await self._request("GET", "/panel/api/inbounds")
        if not isinstance(resp_obj, list):
            raise XUIAPIError("Unexpected inbounds payload format.")
        return [dict(item) for item in resp_obj]

    async def resolve_inbound_id(self, tag_or_remark: str = "vless_reality") -> int:
        target = tag_or_remark.strip().lower()
        inbounds = await self.get_inbounds()
        matches = []
        for inbound in inbounds:
            tag = str(inbound.get("tag") or "").strip().lower()
            remark = str(inbound.get("remark") or "").strip().lower()
            if target in {tag, remark}:
                matches.append(inbound)

        if not matches:
            raise XUIAPIError(f"Inbound '{tag_or_remark}' not found.")
        if len(matches) > 1:
            raise XUIAPIError(f"Inbound '{tag_or_remark}' is ambiguous ({len(matches)} matches).")

        inbound_id = matches[0].get("id")
        if not isinstance(inbound_id, int):
            raise XUIAPIError("Inbound id has unexpected type.")

        LOGGER.info("xui.inbound.resolved", extra={"inbound_id": inbound_id, "key": target})
        return inbound_id

    async def add_client(
        self,
        inbound_id: int,
        email: str,
        uuid: str,
        limit_ip: int = 0,
        total_gb: int = 0,
        expiry_ms: int = 0,
    ) -> None:
        payload = {
            "id": inbound_id,
            "settings": json.dumps({
                "clients": [{
                    "id": uuid,
                    "email": email,
                    "limitIp": limit_ip,
                    "enable": True,
                    "totalGB": total_gb,
                    "expiryTime": expiry_ms,
                }]
            }),
        }
        LOGGER.info("xui.client.add.start", extra={"inbound_id": inbound_id, "email": email})
        await self._request("POST", "/panel/api/inbounds/addClient", json_body=payload)
        LOGGER.info("xui.client.add.ok", extra={"inbound_id": inbound_id, "email": email})

    async def _get_inbound_settings(self, inbound_id: int) -> dict:
        inbounds = await self.get_inbounds()
        for inbound in inbounds:
            if inbound.get("id") == inbound_id:
                settings = inbound.get("settings")
                if isinstance(settings, str):
                    return json.loads(settings)
                if isinstance(settings, dict):
                    return settings
                return {}
        raise XUIAPIError(f"Inbound id={inbound_id} not found.")

    async def update_client(self, inbound_id: int, email: str, updates: dict[str, Any]) -> None:
        settings = await self._get_inbound_settings(inbound_id)
        clients = settings.get("clients", [])
        target_client = next((c for c in clients if str(c.get("email")) == email), None)
        if not target_client:
            raise XUIAPIError(f"Client '{email}' not found in inbound {inbound_id}.")

        target_client.update(updates)

        # Безопасно: шлём ВЕСЬ список клиентов обратно, иначе панель затрёт остальных
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": clients}),
        }
        LOGGER.info("xui.client.update.start", extra={"inbound_id": inbound_id, "email": email})
        await self._request("POST", "/panel/api/inbounds/updateClient", json_body=payload)
        LOGGER.info("xui.client.update.ok", extra={"inbound_id": inbound_id, "email": email})

    async def disable_client(self, inbound_id: int, email: str) -> None:
        await self.update_client(inbound_id, email, {"enable": False})

    async def enable_client(self, inbound_id: int, email: str) -> None:
        await self.update_client(inbound_id, email, {"enable": True})

    async def delete_client(self, inbound_id: int, email: str) -> None:
        LOGGER.info("xui.client.delete.start", extra={"inbound_id": inbound_id, "email": email})
        await self._request("POST", "/panel/api/inbounds/delClient", json_body={
            "id": inbound_id,
            "email": email,
        })
        LOGGER.info("xui.client.delete.ok", extra={"inbound_id": inbound_id, "email": email})

    async def reset_client_traffic(self, inbound_id: int, email: str) -> None:
        LOGGER.info("xui.client.reset_traffic.start", extra={"inbound_id": inbound_id, "email": email})
        await self._request("POST", "/panel/api/inbounds/resetClientTraffic", json_body={
            "id": inbound_id,
            "email": email,
        })
        LOGGER.info("xui.client.reset_traffic.ok", extra={"inbound_id": inbound_id, "email": email})

    async def get_client_traffics(self, inbound_id: int, email: str) -> dict[str, Any] | None:
        # API отдаёт пачку по всем клиентам инбаунда. Фильтруем локально.
        raw = await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{inbound_id}")
        if not isinstance(raw, list):
            return raw
        for item in raw:
            if str(item.get("email")) == email:
                return item
        return None

    async def build_or_get_subscription_url(
        self,
        *,
        inbound_id: int | None = None,
        email: str | None = None,
        existing_url: str | None = None,
        sub_id: str | None = None,
    ) -> str:
        if existing_url and existing_url.strip():
            return existing_url.strip()

        resolved_sub_id = (sub_id or "").strip()
        if not resolved_sub_id and inbound_id is not None and email:
            settings = await self._get_inbound_settings(inbound_id)
            clients = settings.get("clients", [])
            for client in clients:
                if str(client.get("email")) == email:
                    resolved_sub_id = str(client.get("subId") or "").strip()
                    break

        if not resolved_sub_id:
            raise XUIAPIError("Subscription URL cannot be built: subId is missing.")

        parsed = urlparse(self._base_url)
        return f"{parsed.scheme}://{parsed.netloc}/sub/{resolved_sub_id}"