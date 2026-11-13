"""Turn token counts into dollars, so the app can show what each review cost."""
from typing import Optional

from app.config import Settings

# Claude API list prices, USD per million tokens (input, output). Thinking is billed as output.
CLAUDE_PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}


def price_per_million(model: str, settings: Settings) -> Optional[tuple[float, float]]:
    """(input, output) USD per million tokens, or None if unknown."""
    if settings.coach_price_input_per_m is not None and settings.coach_price_output_per_m is not None:
        return settings.coach_price_input_per_m, settings.coach_price_output_per_m
    for name, price in CLAUDE_PRICES.items():
        if model == name or model.startswith(name + "-"):
            return price
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int, settings: Settings) -> Optional[float]:
    price = price_per_million(model, settings)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
