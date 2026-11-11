"""tests/fixtures/e74/ 아래 합성 프로필을 읽는다."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "e74"
PROFILES = FIXTURES / "profiles"


def load_profiles() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(PROFILES.glob("P*.json"))]


def profile_json(case_id: str) -> dict[str, Any]:
    """샘플의 `profile` JSON을 복사해 돌려준다. 테스트가 고쳐도 원본에 영향이 없다."""
    for case in load_profiles():
        if case["id"] == case_id:
            return copy.deepcopy(case["profile"])
    raise KeyError(case_id)
