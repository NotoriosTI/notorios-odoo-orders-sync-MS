from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BACKOFF_SCHEDULE = [30, 60, 120, 240, 600]


class WebhookSendError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WebhookSender:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def send(
        self,
        url: str,
        payload: dict,
        webhook_secret: str = "",
        connection_external_id: str = "",
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "X-Odoo-Connection-Id": connection_external_id,
        }
        if webhook_secret:
            headers["X-Webhook-Secret"] = webhook_secret

        try:
            response = await self._http.post(url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(
                "Webhook enviado OK: connection=%s, status=%d",
                connection_external_id,
                response.status_code,
            )
        except httpx.HTTPStatusError as e:
            raise WebhookSendError(
                f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise WebhookSendError(str(e)) from e

    @staticmethod
    def calculate_next_retry(attempt: int) -> int:
        idx = min(attempt, len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[idx]
