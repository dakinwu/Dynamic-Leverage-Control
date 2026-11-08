from decimal import Decimal

from dynamic_leverage.domain.execution import (
    OneXTwoXRecipe,
    TwoXCashRecipe,
    allocation_exposure,
)


def test_two_x_cash_maps_1_2_exposure_to_sixty_forty() -> None:
    allocations = TwoXCashRecipe().allocate(Decimal("1.2"))
    assert allocations[0].weight == Decimal("0.6")
    assert allocations[1].weight == Decimal("0.4")
    assert allocation_exposure(allocations) == Decimal("1.2")


def test_one_x_two_x_maps_1_2_exposure_to_eighty_twenty() -> None:
    allocations = OneXTwoXRecipe().allocate(Decimal("1.2"))
    assert allocations[0].weight == Decimal("0.8")
    assert allocations[1].weight == Decimal("0.2")
    assert allocation_exposure(allocations) == Decimal("1.2")


def test_one_x_two_x_maps_below_one_x_to_base_and_cash() -> None:
    allocations = OneXTwoXRecipe().allocate(Decimal("0.6"))
    assert [(item.instrument, item.weight) for item in allocations] == [
        ("0050", Decimal("0.6")),
        ("00631L", Decimal("0")),
        ("TWD", Decimal("0.4")),
    ]
    assert allocation_exposure(allocations) == Decimal("0.6")
