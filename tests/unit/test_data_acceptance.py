from datetime import UTC, datetime

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.data_acceptance import audit_data


def test_full_numeric_history_does_not_prove_past_knowledge_or_profitability() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine).begin() as session:
        repo = AuditRepository(session)
        now = datetime.now(UTC)
        repo.append_raw_snapshot(
            kind="valuation.taiex.manual",
            source="test-history",
            observed_at=now,
            received_at=now,
            quality="authoritative",
            checksum="test-history",
            payload={
                "eps_methodology": "derived-taiex-close-divided-by-twse-market-pe",
                "rows": [
                    {
                        "as_of": f"{2015 + i // 12}-{i % 12 + 1:02d}-01",
                        "pe_ttm": "20",
                        "market_eps": "800",
                    }
                    for i in range(121)
                ],
            },
        )
        report = audit_data(repo)
        assert report["valid_months_in_window"] == 121
        assert report["numerical_history_ready"] is True
        assert report["historical_research_ready"] is False
        assert report["profitability_demonstrated"] is False
        assert len(report["months_without_publication_evidence"]) == 121
    engine.dispose()
