from pathlib import Path

from dynamic_leverage.infrastructure.providers.finmind import FinMindSupplementalClient


def test_finmind_token_can_be_read_from_combined_runtime_env(tmp_path: Path) -> None:
    path = tmp_path / "runtime.env"
    path.write_text(
        "SJ_API_KEY=redacted\nFINMIND_TOKEN=finmind-only\nTELEGRAM_CHAT_ID=123\n",
        encoding="utf-8",
    )

    assert FinMindSupplementalClient(path)._token() == "finmind-only"


def test_finmind_token_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "runtime.env"
    path.write_text("SJ_API_KEY=redacted\n", encoding="utf-8")

    assert FinMindSupplementalClient(path)._token() is None


def test_finmind_token_travels_in_the_authorization_header(tmp_path: Path) -> None:
    import asyncio
    from datetime import date

    import httpx
    import respx

    path = tmp_path / "runtime.env"
    path.write_text("FINMIND_TOKEN=secret-token\n", encoding="utf-8")
    with respx.mock(assert_all_called=True) as router:
        route = router.get(FinMindSupplementalClient.endpoint).mock(
            return_value=httpx.Response(200, json={"status": 200, "data": []})
        )
        asyncio.run(
            FinMindSupplementalClient(path).dataset(
                "TaiwanStockPrice", data_id="0050", start_date=date(2026, 11, 2)
            )
        )

    request = route.calls.last.request
    # Query strings end up in proxy and server access logs; headers do not.
    assert "token" not in request.url.params
    assert "secret-token" not in str(request.url)
    assert request.headers["Authorization"] == "Bearer secret-token"
