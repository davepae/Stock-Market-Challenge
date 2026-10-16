"""Tests for the pure functions of the Mock Stock Challenge (no network, no files)."""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import risk
from investment_model import (NewsItem, NewsSentiment, PositionContext, TickerScore, combine,
                              position_adjustment, qualifies_for_entry, recommend,
                              score_headline)
from tech_analysis import Analysis, market_regime_ok
from trading_cycle import plan_buys


# --- Helpers --------------------------------------------------------------------

def make_analysis(ticker="AAA", price=105.0, ma20=100.0, rsi=60.0, momentum=7,
                  below_ma50=False, volatility_pct=2.0) -> Analysis:
    return Analysis(ticker=ticker, price=price, ma20=ma20, rsi=rsi,
                    price_vs_ma_pct=(price / ma20 - 1) * 100, roc5_pct=2.0,
                    momentum=momentum, rel_str=1.0, volatility_pct=volatility_pct,
                    ma50=95.0, below_ma50=below_ma50)


def neutral_news(ticker="AAA", score=0.0) -> NewsSentiment:
    return NewsSentiment(ticker, score, "neutral")


def make_score(score=6.0, analysis=None, held=False, earnings_boost=0.0,
               ticker="AAA") -> TickerScore:
    """A TickerScore whose recommendation is derived from the composite score."""
    analysis = analysis or make_analysis(ticker)
    position = PositionContext(10, 100.0, "2026-01-01", 5, 1.0) if held else None
    return TickerScore(ticker, score, recommend(score), float(analysis.momentum), 0.0, 0.0,
                       analysis, neutral_news(ticker), position, earnings_boost=earnings_boost)


# --- 1. stop_levels -------------------------------------------------------------

def test_stop_levels():
    # Normal position: fixed 13% stop, trailing stop not armed below +5%.
    lv = risk.stop_levels("AAA", entry=100, peak=104, stop_pct=13)
    assert lv.stop_loss == pytest.approx(87.0)
    assert lv.stop_pct == 13
    assert lv.trailing_stop is None and not lv.trailing_active
    assert lv.effective_stop == pytest.approx(87.0)

    # Armed exactly at the activation threshold (+5%): trail floors at breakeven.
    lv = risk.stop_levels("AAA", entry=100, peak=105, stop_pct=13)
    assert lv.stop_loss == pytest.approx(87.0)
    assert lv.trailing_stop == pytest.approx(100.0)  # 105 * 0.92 = 96.6 < entry

    # Armed with a high peak: trail = max(entry, peak * (1 - 8%)) = 101.2.
    lv = risk.stop_levels("AAA", entry=100, peak=110, stop_pct=13)
    assert lv.trailing_active
    assert lv.trailing_stop == pytest.approx(101.2)
    assert lv.effective_stop == pytest.approx(101.2)

    # Peak below entry is clamped to entry.
    assert risk.stop_levels("AAA", entry=100, peak=90, stop_pct=13).peak == 100


def test_stop_levels_fallbacks_and_checks():
    # Default stop when neither stop_pct nor volatility is given.
    assert risk.stop_levels("AAA", 100, 100).stop_loss == pytest.approx(
        100 * (1 - config.DEFAULT_STOP_LOSS_PCT / 100))
    # Volatility-derived stop (1% vol -> ~6.7%).
    assert risk.stop_levels("AAA", 100, 100, volatility_pct=1).stop_pct == pytest.approx(6.708, abs=0.01)

    lv = risk.stop_levels("AAA", entry=100, peak=110, stop_pct=13)
    assert risk.check_stops(lv, 105) == ""
    assert risk.check_stops(lv, 101).startswith("trailing stop")
    assert risk.check_stops(risk.stop_levels("AAA", 100, 100, stop_pct=13), 87).startswith("stop loss")


# --- 2. stop_pct_from_volatility -------------------------------------------------

def test_stop_pct_from_volatility():
    assert risk.stop_pct_from_volatility(1) == pytest.approx(6.708, abs=0.01)   # 3 * 1 * sqrt(5)
    assert risk.stop_pct_from_volatility(3) == config.STOP_LOSS_MAX_PCT == 15   # capped
    assert risk.stop_pct_from_volatility(0.5) == config.STOP_LOSS_MIN_PCT == 5  # floored


# --- 3. position_adjustment ------------------------------------------------------
# position_adjustment() works on position context (P&L, age), not on the
# recommendation, so the cases below follow its real rules.

def pos(pnl, days=2):
    return PositionContext(10, 100.0, "2026-01-01", days, pnl)


def test_position_adjustment():
    # No position (e.g. a candidate, or nothing to adjust) or unknown P&L: no change.
    assert position_adjustment("AAA", None) == (0.0, [])
    assert position_adjustment("AAA", pos(None))[0] == 0.0

    # Winner gets a small bonus; flat/small loser none.
    assert position_adjustment("AAA", pos(4.0))[0] == pytest.approx(0.3)
    assert position_adjustment("AAA", pos(-1.0))[0] == 0.0

    # Near the stop (75% of the stop distance): -1. Default stop is 10% -> -7.5%.
    adj, reasons = position_adjustment("AAA", pos(-8.0))
    assert adj == -1.0 and "near stop loss" in reasons
    # The stop is volatility-scaled: 1% vol -> 6.7% stop, so -5.5% is already "near".
    assert position_adjustment("AAA", pos(-5.5), volatility_pct=1)[0] == -1.0
    assert position_adjustment("AAA", pos(-5.5))[0] == 0.0

    # Stale (held long, little gain): -0.5 on top of any winner bonus (+0.3);
    # stacks with near-stop and stays within -2..+1.
    adj, reasons = position_adjustment("AAA", pos(1.0, days=config.STALE_POSITION_DAYS))
    assert adj == pytest.approx(-0.2) and "stale position" in reasons
    assert position_adjustment("AAA", pos(0.0, days=config.STALE_POSITION_DAYS))[0] == -0.5
    assert position_adjustment("AAA", pos(-9.0, days=30))[0] == -1.5
    assert -2.0 <= position_adjustment("AAA", pos(50.0, days=1))[0] <= 1.0


# --- 4. qualifies_for_entry ------------------------------------------------------

def test_qualifies_for_entry():
    assert qualifies_for_entry(make_score(6.0)) is True            # BUY, price 5% above MA
    assert qualifies_for_entry(make_score(5.0)) is False           # HOLD: below BUY threshold
    assert qualifies_for_entry(make_score(7.5)) is True            # STRONG_BUY

    # Price below the 20MA: only a STRONG_BUY with an earnings beat may enter.
    below = make_analysis(price=98.0, ma20=100.0)
    assert qualifies_for_entry(make_score(6.0, below)) is False
    assert qualifies_for_entry(make_score(7.5, below)) is False
    assert qualifies_for_entry(make_score(7.5, below, earnings_boost=1.5)) is True

    # Held tickers never qualify.
    assert qualifies_for_entry(make_score(6.0, held=True)) is False


def test_qualifies_for_entry_cooldown_and_ma50():
    s = make_score(6.0)
    recent = {"last_sell_date": date.today().isoformat(), "last_sell_reason": "discretionary"}
    assert qualifies_for_entry(s, recent) is False
    # A stop-loss exit does not trigger the cooldown, and an old sell has expired.
    assert qualifies_for_entry(s, {**recent, "last_sell_reason": "stop"}) is True
    old = (date.today() - timedelta(days=30)).isoformat()
    assert qualifies_for_entry(s, {"last_sell_date": old, "last_sell_reason": "discretionary"}) is True

    # Below the 50-day MA: never enter, even a STRONG_BUY.
    assert qualifies_for_entry(make_score(7.5, make_analysis(below_ma50=True))) is False
    # Overbought RSI blocks entry.
    assert qualifies_for_entry(make_score(7.5, make_analysis(rsi=85))) is False


# --- 5. score_headline -----------------------------------------------------------

def test_score_headline():
    pos_score, pos_hits = score_headline("Apple beats expectations in third quarter")
    assert pos_score > 0 and any(h.startswith("+") for h in pos_hits)

    neg_score, neg_hits = score_headline("Apple misses estimates as iPhone sales slow")
    assert neg_score < 0 and any(h.startswith("-") for h in neg_hits)

    assert score_headline("Apple to hold annual shareholder meeting on Tuesday")[0] == 0
    assert -3 <= score_headline("Stock soars on record revenue and raised guidance")[0] <= 3


# --- 6. plan_buys ----------------------------------------------------------------

def candidates(n, price=50.0):
    return [make_score(6.0, make_analysis(f"T{i}", price=price), ticker=f"T{i}") for i in range(n)]


def test_plan_buys_max_new_buys_per_cycle():
    decisions = plan_buys(candidates(6), slots=6, cash=1_000_000, total_value=1_000_000)
    assert len(decisions) == config.MAX_NEW_BUYS_PER_CYCLE == 3
    assert [d.ticker for d in decisions] == ["T0", "T1", "T2"]  # best-first order kept


def test_plan_buys_respects_slots():
    assert len(plan_buys(candidates(6), slots=1, cash=1_000_000, total_value=1_000_000)) == 1
    assert plan_buys(candidates(6), slots=0, cash=1_000_000, total_value=1_000_000) == []
    # MAX_POSITIONS: a full book (10 held) leaves no slots.
    assert config.MAX_POSITIONS == 10
    assert plan_buys(candidates(3), config.MAX_POSITIONS - 10, 1_000_000, 1_000_000) == []


def test_plan_buys_max_position_pct():
    total = 1_000_000
    decisions = plan_buys(candidates(1), slots=3, cash=total, total_value=total)
    assert len(decisions) == 1
    value = decisions[0].shares * decisions[0].price
    assert value <= total * config.MAX_POSITION_PCT / 100 + 1e-6
    assert value == pytest.approx(total * 0.18, rel=1e-3)  # cash is plentiful: capped at 18%


def test_plan_buys_min_cash_reserve():
    total = 1_000_000
    # Plenty of candidates, all cash: total spend must leave at least 5% in cash.
    decisions = plan_buys(candidates(3), slots=3, cash=total, total_value=total)
    spent = sum(d.shares * d.price for d in decisions)
    assert total - spent >= total * config.MIN_CASH_PCT / 100 - 1e-6

    # Cash already at/below the reserve: nothing is bought.
    assert plan_buys(candidates(3), 3, cash=total * 0.05, total_value=total) == []
    assert plan_buys(candidates(3), 3, cash=total * 0.05 + 100, total_value=total) == []  # < MIN_TRADE_VALUE

    # Limited cash: spend is limited to cash above the reserve.
    cash = 200_000
    decisions = plan_buys(candidates(3), 3, cash=cash, total_value=total)
    assert sum(d.shares * d.price for d in decisions) <= cash - total * 0.05 + 1e-6


# --- 7. market regime ------------------------------------------------------------

def test_market_regime():
    spy_ok = make_analysis("SPY", below_ma50=False)
    spy_bad = make_analysis("SPY", below_ma50=True)
    assert market_regime_ok(spy_ok) is True
    assert market_regime_ok(spy_bad) is False
    assert market_regime_ok(None) is True  # fails open without benchmark data

    strong = make_score(7.5)
    assert qualifies_for_entry(strong, market_ok=market_regime_ok(spy_ok)) is True
    assert qualifies_for_entry(strong, market_ok=market_regime_ok(spy_bad)) is False


# --- 8. news score cap -----------------------------------------------------------

def composite(news_score: float) -> float:
    return combine("AAA", make_analysis(momentum=6), neutral_news(score=news_score), None).score


def test_news_score_cap():
    assert config.NEWS_SCORE_CAP == 1
    # Beyond +/-1 the news no longer moves the composite.
    assert composite(5.0) == composite(1.0) == composite(3.0)
    assert composite(-5.0) == composite(-1.0) == composite(-3.0)
    # Within the cap it still does, by NEWS_WEIGHT per point.
    assert composite(1.0) - composite(0.0) == pytest.approx(config.NEWS_WEIGHT, abs=0.11)
    assert composite(1.0) > composite(0.0) > composite(-1.0)
    # Max swing from news is 2 * cap * NEWS_WEIGHT = 0.6.
    assert composite(5.0) - composite(-5.0) == pytest.approx(0.6, abs=0.11)
    # The raw news score is still reported unchanged.
    assert combine("AAA", make_analysis(), neutral_news(score=5.0), None).news_score == 5.0
