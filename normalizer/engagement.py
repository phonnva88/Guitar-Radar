"""The post feed does not distinguish missing counts from zero in its legacy fields."""
import re


def engagement_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:/+=-]{1,256}", value) else None


def observed_count(value):
    return value if type(value) is int and 0 <= value <= 2147483647 else None


def post_engagement(value):
    if not isinstance(value, dict):
        return None
    feedback_id = engagement_id(value.get("feedback_id"))
    if feedback_id is None:
        return None
    return {"feedback_id": feedback_id,
            "comment_count": observed_count(value.get("comment_count")),
            "reaction_count": observed_count(value.get("reaction_count"))}
