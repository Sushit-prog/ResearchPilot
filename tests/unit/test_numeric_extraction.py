from __future__ import annotations

import pytest

from app.agent.researcher import extract_numeric


def test_percent_pattern_wins_when_earliest() -> None:
    fact = extract_numeric("up 12% to $8.9B in Q4")
    assert fact is not None
    assert fact.value == 12.0
    assert fact.unit == "percent"
    assert fact.metric_label == "up"


def test_currency_fact_without_percent() -> None:
    fact = extract_numeric("Revenue hit $8.9B in Q4")
    assert fact is not None
    assert fact.value == pytest.approx(8.9e9)
    assert fact.unit == "USD_B"
    assert fact.metric_label == "revenue hit"


def test_currency_fact_comma_and_cents() -> None:
    fact = extract_numeric("The order was $1,250.50 at close")
    assert fact is not None
    assert fact.value == pytest.approx(1250.50)
    assert fact.unit == "USD"


def test_magnitude_noun_pattern() -> None:
    fact = extract_numeric("The app reached 350 million users")
    assert fact is not None
    assert fact.value == 350.0
    assert fact.unit == "million_users"
    assert fact.metric_label == "app reached"


def test_prose_without_numeric_pattern_returns_none() -> None:
    assert extract_numeric("The launch went well and the crew is safe.") is None


def test_label_uses_at_most_three_significant_tokens() -> None:
    fact = extract_numeric("Total company annual revenue grew 12% year over year")
    assert fact is not None
    assert fact.metric_label == "annual revenue grew"


def test_label_none_when_match_starts_the_text() -> None:
    fact = extract_numeric("12% growth reported")
    assert fact is not None
    assert fact.metric_label is None
