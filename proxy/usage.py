"""Input usage accounting for Anthropic and explicit OpenAI converter metadata.

Never guess whether an unmarked input_tokens includes cache: native Anthropic
fields are disjoint. Legacy converters retaining OpenAI details explicitly mark
an inclusive total. New cc-router removes those details after normalization.
"""
import math


def token_count(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        count = int(value)
        return count if count >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class InputUsage:
    """Merge partial stream usage; output-only deltas must not erase input."""
    def __init__(self):
        self.fields = {}
        self.total = 0
        self.cache_read = None
        self.cache_creation = 0
        self.fresh = 0

    def update(self, usage):
        if not isinstance(usage, dict):
            return False
        changed = False
        for key in ("input_tokens", "prompt_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            if key in usage and token_count(usage[key]) is not None:
                self.fields[key] = token_count(usage[key])
                changed = True
        for key in ("input_tokens_details", "prompt_tokens_details"):
            details = usage.get(key)
            if isinstance(details, dict):
                for field in ("cached_tokens", "cache_write_tokens"):
                    count = token_count(details.get(field))
                    if count is not None:
                        self.fields.setdefault(key, {})[field] = count
                        changed = True
        if not changed:
            return False
        details = self.fields.get("input_tokens_details", {})
        prompt_details = self.fields.get("prompt_tokens_details", {})
        detail_read = details.get("cached_tokens", prompt_details.get("cached_tokens"))
        detail_write = details.get("cache_write_tokens", prompt_details.get("cache_write_tokens"))
        read = self.fields.get("cache_read_input_tokens", detail_read)
        write = self.fields.get("cache_creation_input_tokens", detail_write or 0)
        base = self.fields.get("input_tokens", self.fields.get("prompt_tokens", 0))
        inclusive = detail_read is not None or detail_write is not None or "prompt_tokens" in self.fields
        if inclusive and ("input_tokens" in self.fields or "prompt_tokens" in self.fields):
            self.total = base
            self.cache_read = min(read, base) if read is not None else None
            self.cache_creation = min(write, base - (self.cache_read or 0))
            self.fresh = base - (self.cache_read or 0) - self.cache_creation
        else:
            self.cache_read = read
            self.cache_creation = write
            self.fresh = base
            self.total = base + (read or 0) + write
        return True
