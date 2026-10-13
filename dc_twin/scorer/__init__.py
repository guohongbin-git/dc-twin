"""dc_twin.scorer — 独立评分器 (C2 Oracle 独立契约)。"""
from __future__ import annotations

from .oracle import OracleReport, OpVerdict, score_run

__all__ = ["OracleReport", "OpVerdict", "score_run"]
