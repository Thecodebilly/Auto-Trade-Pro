from autotrade_pro.market_data import MarketDataBundle, MarketSignal
from autotrade_pro.valuation import calculate_valuation, score_condition


def test_offer_never_exceeds_retail_cap():
    market = MarketDataBundle(
        region="south_florida",
        auction_value=25000,
        retail_value=20000,
        comparable_value=26000,
        confidence=0.92,
        signals=[
            MarketSignal(
                source="test",
                retail_value=20000,
                wholesale_value=25000,
                sample_size=20,
                days_supply=30,
                confidence=0.9,
                raw={},
            )
        ],
        notes=[],
    )
    result = calculate_valuation(
        dealer={"max_retail_percent": 0.95, "valuation_hold_days": 10},
        vehicle={"vin": "1HGCM82633A004352", "year": 2024, "make": "HONDA", "model": "ACCORD"},
        mileage=1000,
        condition_answers={
            "dents": "none",
            "interior": "clean",
            "warning_lights": "none",
            "tires": "0_6",
            "brakes": "0_6",
            "oil_change": "0_3",
        },
        photo_labels=["front", "rear", "interior", "dash", "tires"],
        market=market,
    )

    assert result.trade_offer <= 19000
    assert result.cap_value == 19000


def test_condition_score_penalizes_missing_photos_and_damage():
    score, grade, adjustments = score_condition(
        {
            "dents": "major",
            "interior": "tears",
            "warning_lights": "check_engine",
            "tires": "over_36",
            "brakes": "unknown",
            "oil_change": "over_12",
        },
        ["front"],
    )

    assert score < 62
    assert grade == "Needs Review"
    assert "photo_completeness" in adjustments["penalties"]
