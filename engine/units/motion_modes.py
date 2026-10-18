"""Small, side-effect-free helpers for Pipeline video routing."""

from __future__ import annotations

from typing import Any, Iterable


INTRO_I2V_T2V_MODE = "intro_i2v_t2v"


def split_intro_i2v_t2v_clip_ids(
    clips: Iterable[dict[str, Any]],
    intro_duration: float,
) -> tuple[list[int], list[int]]:
    """Split montage clip IDs at the intro boundary.

    A clip belongs to the intro when its start is before the boundary. This is
    the same rule used by the existing intro/vislide gates.
    """
    try:
        boundary = max(0.0, float(intro_duration))
    except (TypeError, ValueError):
        boundary = 0.0

    intro_ids: list[int] = []
    t2v_ids: list[int] = []
    for clip in clips:
        try:
            clip_id = int(clip["id"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            start_time = float(clip.get("startTime", 0) or 0)
        except (TypeError, ValueError):
            start_time = 0.0
        if boundary > 0 and start_time < boundary:
            intro_ids.append(clip_id)
        else:
            t2v_ids.append(clip_id)

    return intro_ids, t2v_ids
