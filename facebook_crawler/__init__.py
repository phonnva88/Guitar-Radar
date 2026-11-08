"""Facebook acquisition module: validated, sanitized collector envelopes only."""
from .collector import CollectionFailure, FacebookCrawler, safe_code, write_json

__all__ = ["CollectionFailure", "FacebookCrawler", "safe_code", "write_json"]
