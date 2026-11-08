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
