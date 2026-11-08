"""Scope checks for user-confirmed post fixtures."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from normalizer import FACEBOOK_HOSTS, timestamp


def numeric_group_from_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if not parsed.hostname or parsed.hostname.lower() not in FACEBOOK_HOSTS:
        return None
    match = re.fullmatch(r"/groups/([0-9]+)/(?:posts|permalink)/[^/]+/?", parsed.path)
    return match.group(1) if match else None


def assess_fixture(item: dict, group_id: object, bounds: dict | None, *, observed: bool) -> dict:
    """Return independently checkable group/window scope for one fixture."""
    fixture_group = numeric_group_from_url(item.get("url"))
    target_group = str(group_id) if isinstance(group_id, (str, int)) and str(group_id).isdigit() else None
    if fixture_group and target_group:
        if fixture_group == target_group:
            group_status, group_reason = "verified_same_group", "numeric_group_id_matches"
        else:
            group_status, group_reason = "wrong_group", "numeric_group_id_mismatch"
    else:
        group_status = "group_unverified"
        if item.get("unresolved"):
            group_reason = "share_alias_unresolved"
        elif item.get("post_id") and not item.get("url"):
            group_reason = "post_id_without_group_url"
        elif not target_group:
            group_reason = "target_group_unresolved"
        else:
            group_reason = "permalink_has_no_numeric_group_id"

    window_status = "not_assessed"
    verified_at = timestamp(item.get("verified_posted_at"))
    if group_status == "verified_same_group":
        if verified_at and bounds:
            start = datetime.fromisoformat(bounds["from"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(bounds["to"].replace("Z", "+00:00"))
            instant = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            if instant < start or instant > end:
                window_status = "outside_window_verified"
            else:
                window_status = "observed_in_window" if observed else "not_observed"
        elif observed:
            # The normalized collector sample itself carries a validated in-window timestamp.
            window_status = "observed_in_window"
        else:
            window_status = "timestamp_unknown"

    eligible = group_status == "verified_same_group" and window_status in {"observed_in_window", "not_observed"}
    return {"group_status": group_status, "group_reason": group_reason,
            "fixture_group_id": fixture_group, "window_status": window_status,
            "verified_posted_at": verified_at, "eligible_for_window": eligible,
            "unexpected_observation_outside_verified_window": window_status == "outside_window_verified" and observed}

