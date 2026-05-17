"""Anthropic per-million-token pricing for Claude models.

Verified from platform.claude.com/docs/en/about-claude/pricing.
All prices in USD per million tokens.
"""

PRICING = {
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0, "cache_r": 0.50, "cache_w_5m": 6.25,  "cache_w_1h": 10.0},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0, "cache_r": 0.50, "cache_w_5m": 6.25,  "cache_w_1h": 10.0},
    "claude-opus-4-5":   {"in": 5.0,  "out": 25.0, "cache_r": 0.50, "cache_w_5m": 6.25,  "cache_w_1h": 10.0},
    "claude-opus-4-1":   {"in": 15.0, "out": 75.0, "cache_r": 1.50, "cache_w_5m": 18.75, "cache_w_1h": 30.0},
    "claude-opus-4":     {"in": 15.0, "out": 75.0, "cache_r": 1.50, "cache_w_5m": 18.75, "cache_w_1h": 30.0},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "cache_r": 0.30, "cache_w_5m": 3.75,  "cache_w_1h": 6.0},
    "claude-sonnet-4-5": {"in": 3.0,  "out": 15.0, "cache_r": 0.30, "cache_w_5m": 3.75,  "cache_w_1h": 6.0},
    "claude-sonnet-4":   {"in": 3.0,  "out": 15.0, "cache_r": 0.30, "cache_w_5m": 3.75,  "cache_w_1h": 6.0},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "cache_r": 0.10, "cache_w_5m": 1.25,  "cache_w_1h": 2.0},
    "claude-haiku-3-5":  {"in": 0.80, "out": 4.0,  "cache_r": 0.08, "cache_w_5m": 1.0,   "cache_w_1h": 1.6},
}


def get_pricing(model):
    """Match a model string to its pricing entry (handles versioned IDs)."""
    if not model:
        return None
    for key, val in PRICING.items():
        if key in model:
            return val
    return None


def call_cost(usage, model):
    """Compute total cost for one API call from its usage object."""
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        usage.get("input_tokens", 0) / 1e6 * p["in"]
        + usage.get("output_tokens", 0) / 1e6 * p["out"]
        + usage.get("cache_read_input_tokens", 0) / 1e6 * p["cache_r"]
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * p["cache_w_5m"]
    )
