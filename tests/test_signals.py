from wealth_agents.signals import as_int_map, phase25_risk_off_score, risk_off_score, risk_on_score, top_regime_drivers


def test_scores_match_weighted_counts() -> None:
    aggregate = {
        "category_counts": {
            "macroeconomics": 3,
            "rates": 2,
            "equities": 5,
        },
        "keyword_counts": {
            "inflation": 4,
            "rate hike": 1,
            "recession": 1,
            "growth": 3,
            "earnings": 2,
            "rate cut": 1,
        },
    }

    assert risk_off_score(aggregate) == 11
    assert risk_on_score(aggregate) == 11
    assert phase25_risk_off_score(aggregate) == 5


def test_top_regime_drivers_is_sorted_and_limited() -> None:
    aggregate = {
        "category_counts": {
            "equities": 4,
            "macroeconomics": 2,
            "rates": 1,
        },
        "keyword_counts": {
            "inflation": 3,
            "growth": 2,
            "earnings": 2,
            "rate hike": 1,
        },
    }

    rows = top_regime_drivers(aggregate, limit=4)
    assert [row["term"] for row in rows] == [
        "equities",
        "inflation",
        "macroeconomics",
        "earnings",
    ]
    assert [row["contribution"] for row in rows] == [4, 3, 2, 2]


def test_as_int_map_normalizes_keys_and_filters_invalid_values() -> None:
    raw = {
        " Inflation ": "2",
        "RATE HIKE": 1,
        "good": "x",
        42: 7,
    }
    assert as_int_map(raw) == {
        "inflation": 2,
        "rate hike": 1,
    }
