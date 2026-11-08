from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx


@dataclass(frozen=True, slots=True)
class HttpSnapshot:
    source: str
    received_at: datetime
    body: bytes
    content_type: str
    checksum: str


class HttpSnapshotClient:
    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout = httpx.Timeout(timeout_seconds)

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> HttpSnapshot:
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        return HttpSnapshot(
            source=url,
            received_at=datetime.now(ZoneInfo("Asia/Taipei")),
            body=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            checksum=hashlib.sha256(response.content).hexdigest(),
        )
