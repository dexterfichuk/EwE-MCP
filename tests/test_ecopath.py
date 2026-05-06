import pytest
from ewe_mcp.ecopath import (
    estimate_average_longevity,
    estimate_catch,
    estimate_consumption,
    estimate_fishing_mortality,
    estimate_production,
    estimate_total_mortality,
)


def test_fishing_mortality_calculation():
    # F = C / B
    assert estimate_fishing_mortality(catch=10, biomass=100) == 0.1
    assert estimate_fishing_mortality(catch=0, biomass=100) == 0.0
    assert estimate_fishing_mortality(catch=0, biomass=0) == 0.0


def test_fishing_mortality_zero_biomass_error():
    with pytest.raises(ValueError, match="Biomass cannot be zero"):
        estimate_fishing_mortality(catch=10, biomass=0)


def test_total_mortality_calculation():
    # Z = F + M
    assert estimate_total_mortality(fishing_mortality=0.2, natural_mortality=0.3) == 0.5
    assert estimate_total_mortality(fishing_mortality=0.0, natural_mortality=0.5) == 0.5


def test_catch_calculation():
    # C = B * F
    assert estimate_catch(biomass=100, fishing_mortality=0.1) == 10.0
    assert estimate_catch(biomass=0, fishing_mortality=0.1) == 0.0
    assert estimate_catch(biomass=100, fishing_mortality=0.0) == 0.0


def test_production_calculation():
    # P = B * (P/B)
    assert estimate_production(biomass=100, pb_ratio=0.5) == 50.0


def test_consumption_calculation():
    # Q = B * (Q/B)
    assert estimate_consumption(biomass=100, qb_ratio=10.0) == 1000.0


def test_longevity_calculation():
    # Longevity = 1 / (P/B)
    assert estimate_average_longevity(pb_ratio=0.5) == 2.0
    with pytest.raises(ValueError, match="P/B ratio cannot be zero"):
        estimate_average_longevity(pb_ratio=0.0)
