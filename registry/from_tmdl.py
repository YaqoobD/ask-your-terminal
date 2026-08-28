"""Parses a Power BI TMDL export's measure blocks into registry-comparable
entries, so the canonical registry can be checked against what the model
actually shipped instead of trusted blindly.
"""

from __future__ import annotations

import re
from pathlib import Path

MEASURE_RE = re.compile(r"^\s*measure\s+'([^']+)'\s*=\s*(.+)$")
DESCRIPTION_RE = re.compile(r"^\s*description:\s*(.+)$")

# TMDL measure display name -> canonical registry metric key. Fixed mapping
# because TMDL names are free text; nothing to infer generically.
NAME_TO_METRIC_KEY = {
    "Dwell Time": "dwell_time",
    "Crane Idle %": "crane_idle_pct",
    "Gate Turnaround": "gate_turnaround",
}


def parse_tmdl(path: Path) -> list[dict]:
    measures = []
    current = None
    for line in path.read_text().splitlines():
        measure_match = MEASURE_RE.match(line)
        if measure_match:
            if current is not None:
                measures.append(current)
            name = measure_match.group(1)
            current = {
                "name": name,
                "expression": measure_match.group(2).strip(),
                "description": "",
                "metric_key": NAME_TO_METRIC_KEY.get(name),
            }
            continue
        description_match = DESCRIPTION_RE.match(line)
        if description_match and current is not None:
            current["description"] = description_match.group(1).strip()
    if current is not None:
        measures.append(current)
    return measures
