"""Ecological and Ecopath-specific mathematical formulas."""

from __future__ import annotations

import math


def _check_finite(*values: float) -> None:
    for v in values:
        if not math.isfinite(v):
            raise ValueError(f"Value must be a finite number, got {v}")


def _check_non_negative(*values: float, names: list[str] | None = None) -> None:
    for i, v in enumerate(values):
        if v < 0:
            name = names[i] if names else f"parameter {i}"
            raise ValueError(f"{name} must be non-negative, got {v}")


def estimate_fishing_mortality(catch: float, biomass: float) -> float:
    """
    Estimate fishing mortality (F) as Catch (C) / Biomass (B).

    F = C / B
    """
    _check_finite(catch, biomass)
    _check_non_negative(catch, biomass, names=["catch", "biomass"])
    if biomass == 0:
        if catch == 0:
            return 0.0
        raise ValueError("Biomass cannot be zero when catch is non-zero.")
    return catch / biomass


def estimate_total_mortality(
    fishing_mortality: float, natural_mortality: float
) -> float:
    """
    Estimate total mortality (Z) as the sum of fishing mortality (F) and natural mortality (M).

    Z = F + M
    """
    _check_finite(fishing_mortality, natural_mortality)
    _check_non_negative(fishing_mortality, natural_mortality, names=["fishing_mortality", "natural_mortality"])
    return fishing_mortality + natural_mortality


def estimate_catch(biomass: float, fishing_mortality: float) -> float:
    """
    Estimate catch (C) as Biomass (B) * Fishing Mortality (F).

    C = B * F
    """
    _check_finite(biomass, fishing_mortality)
    _check_non_negative(biomass, fishing_mortality, names=["biomass", "fishing_mortality"])
    return biomass * fishing_mortality


def estimate_production(biomass: float, pb_ratio: float) -> float:
    """
    Estimate total production (P) as Biomass (B) * Production/Biomass ratio (P/B).

    P = B * (P/B)
    """
    _check_finite(biomass, pb_ratio)
    _check_non_negative(biomass, pb_ratio, names=["biomass", "pb_ratio"])
    return biomass * pb_ratio


def estimate_consumption(biomass: float, qb_ratio: float) -> float:
    """
    Estimate total consumption (Q) as Biomass (B) * Consumption/Biomass ratio (Q/B).

    Q = B * (Q/B)
    """
    _check_finite(biomass, qb_ratio)
    _check_non_negative(biomass, qb_ratio, names=["biomass", "qb_ratio"])
    return biomass * qb_ratio


def estimate_average_longevity(pb_ratio: float) -> float:
    """
    Estimate average longevity as the inverse of the P/B ratio (expressed in years).

    Longevity = 1 / (P/B)
    """
    _check_finite(pb_ratio)
    if pb_ratio <= 0:
        raise ValueError(f"P/B ratio must be positive when estimating longevity, got {pb_ratio}")
    return 1.0 / pb_ratio
