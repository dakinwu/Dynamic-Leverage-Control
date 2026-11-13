"""Leveraged-ETF exposure is authoritative only from a current, plausible PCF."""

from decimal import Decimal

import pytest
from test_account_sync import FakeAccountReader, _snapshot

from dynamic_leverage.infrastructure.database import Base, make_engine, make_session_factory
from dynamic_leverage.infrastructure.repository import AuditRepository
from dynamic_leverage.services.account_sync import sync_account_snapshot

# Account snapshot: 2026-08-31 13:30 Taipei (Monday); previous session 2026-08-28.
DISCLAIMER = (
    "【本基金為槓桿型指數股票型基金，且以做多期貨為主要交易，其整體曝險部位不得低於"
    "本基金淨資產價值之180%，且不得超過220%，詳情請參閱公開說明書】"
)


def _yuanta_holdings(trade_date: str, futures: str = "160.82", stock: str = "36.23") -> str:
    """Text layout of the issuer's 00631L PCF page (holdings weights in %)."""
    return (
        f"{DISCLAIMER} 基金權重-期貨 交易日期: {trade_date} 商品代碼 商品名稱 商品數量 "
        "商品權重 商品年月 商品代碼 TX 商品名稱 臺股期貨 商品數量 54515 "
        f"商品權重 {futures} 商品年月 202609 商品代碼 NYF 商品名稱 台灣50ETF股票期貨 "
        "商品數量 8800 商品權重 3.04 商品年月 202609 基金權重-股票 "
        f"交易日期: {trade_date} 商品代碼 商品名稱 商品數量 商品權重 商品代碼 2330 "
        f"商品名稱 台積電 商品數量 45587000 商品權重 {stock} 匯率 交易日期: {trade_date}"
    )


def _sync_with_pcf(body: str):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    leveraged = _snapshot(code="00631L")
    try:
        with make_session_factory(engine).begin() as session:
            AuditRepository(session).append_raw_snapshot(
                kind="market.yuanta_00631l_pcf.official",
                source="https://www.yuantaetfs.com/tradeInfo/pcf/00631L",
                observed_at=leveraged.observed_at,
                received_at=leveraged.observed_at,
                quality="authoritative",
                checksum="p" * 64,
                payload={"body_text": body},
            )
            return sync_account_snapshot(
                session, FakeAccountReader(leveraged), binding="9A00-1234567"
            )
    finally:
        engine.dispose()


def test_holdings_weights_on_a_current_pcf_give_the_exposure() -> None:
    result = _sync_with_pcf(_yuanta_holdings("2026/08/28"))

    assert result.exposure_quality == "authoritative-pcf"
    # 50,000 market value x 2.0009 (160.82% + 3.04% + 36.23%) on 130,000 strategy NAV.
    assert result.actual_exposure == Decimal("50000") * Decimal("2.0009") / Decimal("130000")


def test_prospectus_exposure_band_is_not_read_as_actual_exposure() -> None:
    result = _sync_with_pcf(f"{DISCLAIMER} 交易日期: 2026/08/31")

    assert result.actual_exposure is None
    assert result.exposure_quality != "authoritative-pcf"


@pytest.mark.parametrize("futures", ["260.82", "110.00"])
def test_exposure_outside_the_two_x_band_is_unknown(futures: str) -> None:
    result = _sync_with_pcf(_yuanta_holdings("2026/08/28", futures=futures))

    assert result.actual_exposure is None
    assert result.exposure_quality == "pcf-exposure-out-of-range"


def test_pcf_from_an_earlier_session_is_unknown() -> None:
    result = _sync_with_pcf(_yuanta_holdings("2026/08/20"))

    assert result.actual_exposure is None
    assert result.exposure_quality == "pcf-stale"


def test_explicit_actual_exposure_still_needs_a_trade_date() -> None:
    undated = _sync_with_pcf("實際曝險 200%")
    dated = _sync_with_pcf("交易日期: 2026/08/31 實際曝險 200%")

    assert undated.actual_exposure is None
    assert dated.exposure_quality == "authoritative-pcf"
