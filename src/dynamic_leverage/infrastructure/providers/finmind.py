from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx


class FinMindSupplementalClient:
    """Supplemental history only; responses never authorize an exposure increase."""

    endpoint = "https://api.finmindtrade.com/api/v4/data"

    def __init__(self, token_file: Path | None = None) -> None:
        self.token_file = token_file

    def _token(self) -> str | None:
        if self.token_file is None or not self.token_file.exists():
            return None
        lines = [
            line.strip()
            for line in self.token_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for line in lines:
            if line.startswith("FINMIND_TOKEN="):
                return line.split("=", 1)[1].strip() or None
        if len(lines) == 1 and "=" not in lines[0]:
            return lines[0]
        return None

    async def dataset(
        self,
        dataset: str,
        *,
        data_id: str,
        start_date: date,
        end_date: date | None = None,
    ) -> list[dict[str, object]]:
        params = {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": start_date.isoformat(),
        }
        if end_date is not None:
            params["end_date"] = end_date.isoformat()
        token = self._token()
        # Header, not query string: URLs are written to proxy and access logs.
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(self.endpoint, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") != 200:
            message = payload.get("msg", "unknown")
            raise RuntimeError(f"FinMind supplemental request failed: {message}")
        return list(payload.get("data", []))
