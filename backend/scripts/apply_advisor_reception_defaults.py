"""Apply the approved advisor-flow configuration without changing operator thresholds."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.reception_config import (
    ReceptionConfiguration,
    get_reception_configuration,
    put_reception_configuration,
)


def run() -> dict:
    with SessionLocal() as db:
        before = get_reception_configuration(db)
        updated = ReceptionConfiguration.model_validate(before)
        updated.lead_capture.require_party_size = False
        updated.lead_capture.require_departure_window = False
        value = put_reception_configuration(db, updated)
        db.commit()
    return {
        "large_group_minimum_preserved": value["handoff"]["large_group_minimum"],
        "require_party_size": value["lead_capture"]["require_party_size"],
        "require_departure_window": value["lead_capture"]["require_departure_window"],
        "outbound": False,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
