from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfilePreset:
    name: str
    target_net_edge_pct: float
    safety_margin_pct: float
    max_active_orders: int
    levels_min: int
    levels_max: int
    min_range_pct: float
    max_range_pct: float


PROFILE_PRESETS: dict[str, ProfilePreset] = {
    "CONSERVATIVE": ProfilePreset(
        name="CONSERVATIVE",
        target_net_edge_pct=0.08,
        safety_margin_pct=0.05,
        max_active_orders=8,
        levels_min=2,
        levels_max=6,
        min_range_pct=2.5,
        max_range_pct=7.0,
    ),
    "BALANCED": ProfilePreset(
        name="BALANCED",
        target_net_edge_pct=0.05,
        safety_margin_pct=0.03,
        max_active_orders=12,
        levels_min=3,
        levels_max=10,
        min_range_pct=1.5,
        max_range_pct=6.0,
    ),
    "AGGRESSIVE": ProfilePreset(
        name="AGGRESSIVE",
        target_net_edge_pct=0.03,
        safety_margin_pct=0.01,
        max_active_orders=16,
        levels_min=4,
        levels_max=14,
        min_range_pct=0.8,
        max_range_pct=5.0,
    ),
}


def get_profile_preset(profile: str) -> ProfilePreset:
    return PROFILE_PRESETS.get(profile.upper(), PROFILE_PRESETS["BALANCED"])
