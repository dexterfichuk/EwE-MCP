"""Ecological and Ecopath-specific mathematical formulas."""

from __future__ import annotations


def estimate_fishing_mortality(catch: float, biomass: float) -> float:
    """
    Estimate fishing mortality (F) as Catch (C) / Biomass (B).

    F = C / B
    """
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
    return fishing_mortality + natural_mortality


def estimate_catch(biomass: float, fishing_mortality: float) -> float:
    """
    Estimate catch (C) as Biomass (B) * Fishing Mortality (F).

    C = B * F
    """
    return biomass * fishing_mortality


def estimate_production(biomass: float, pb_ratio: float) -> float:
    """
    Estimate total production (P) as Biomass (B) * Production/Biomass ratio (P/B).

    P = B * (P/B)
    """
    return biomass * pb_ratio


def estimate_consumption(biomass: float, qb_ratio: float) -> float:
    """
    Estimate total consumption (Q) as Biomass (B) * Consumption/Biomass ratio (Q/B).

    Q = B * (Q/B)
    """
    return biomass * qb_ratio


def estimate_average_longevity(pb_ratio: float) -> float:
    """
    Estimate average longevity as the inverse of the P/B ratio (expressed in years).

    Longevity = 1 / (P/B)
    """
    if pb_ratio == 0:
        raise ValueError("P/B ratio cannot be zero when estimating longevity.")
    return 1.0 / pb_ratio
