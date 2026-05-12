"""Ecological data source clients for EwE parameterization."""

from ewe_mcp.data_sources.fishbase import query_fishbase
from ewe_mcp.data_sources.globi import query_globi
from ewe_mcp.data_sources.sealifebase import query_sealifebase

__all__ = ["query_fishbase", "query_sealifebase", "query_globi"]
