"""The deterministic query intent. No tenant field, no client field, no table
field: the schema cannot express a tenant, so nothing upstream of the compiler
has a tenant to set or override. compile() takes tenant_id separately, from
the authenticated session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, model_validator

_CANONICAL_PATH = Path(__file__).parent.parent / "registry" / "canonical_metrics.yml"
_CANONICAL = yaml.safe_load(_CANONICAL_PATH.read_text())
METRIC_NAMES = tuple(_CANONICAL["metrics"].keys())

Metric = Literal[METRIC_NAMES]  # unknown metric names can't validate
Grain = Literal["day", "week", "month"]
Op = Literal["aggregate", "diagnose"]


class TimeWindow(BaseModel):
    start: str
    end: str


class SortSpec(BaseModel):
    by: Literal["value", "dimension"] = "value"
    direction: Literal["asc", "desc"] = "desc"


class QueryIntent(BaseModel):
    op: Op
    metric: Metric
    grain: Grain = "day"
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    time_window: Union[str, TimeWindow]
    sort: Optional[SortSpec] = None
    limit: Optional[int] = None
    as_of: Optional[str] = None

    @model_validator(mode="after")
    def _check_dimensions_allowed(self) -> "QueryIntent":
        allowed = set(_CANONICAL["metrics"][self.metric]["allowed_dimensions"])
        unknown = set(self.dimensions) - allowed
        if unknown:
            raise ValueError(
                f"metric '{self.metric}' does not allow dimension(s) {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            )
        return self

    @model_validator(mode="after")
    def _check_grain_allowed(self) -> "QueryIntent":
        allowed = set(_CANONICAL["metrics"][self.metric]["allowed_grains"])
        if self.grain not in allowed:
            raise ValueError(f"metric '{self.metric}' does not allow grain '{self.grain}'; allowed: {sorted(allowed)}")
        return self
