from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from data_provider.market_clock import MarketClock, MarketPhase
from src.core import trading_calendar


def _fixed_calendar(*sessions: date):
    session_dates = set(sessions)

    def is_trading_day(market: str, check_date: date) -> bool:
        assert market in {"cn", "hk", "us"}
        return check_date in session_dates

    return is_trading_day


@pytest.mark.parametrize(
    ("market", "expected_tz", "expected_segments", "expected_close"),
    [
        (
            "A",
            "Asia/Shanghai",
            (("09:30", "11:30"), ("13:00", "15:00")),
            "15:00",
        ),
        (
            "H",
            "Asia/Hong_Kong",
            (("09:30", "12:00"), ("13:00", "16:00")),
            "16:00",
        ),
        (
            "US",
            "America/New_York",
            (("09:30", "16:00"),),
            "16:00",
        ),
    ],
)
def test_market_sessions_are_timezone_aware_and_market_specific(
    market, expected_tz, expected_segments, expected_close
):
    session_date = date(2026, 1, 5)
    clock = MarketClock(
        market,
        trading_day_resolver=_fixed_calendar(session_date),
    )

    session = clock.session_for(session_date)

    assert session is not None
    assert session.timezone == expected_tz
    assert session.open_at.tzinfo == ZoneInfo(expected_tz)
    assert session.close_at.tzinfo == ZoneInfo(expected_tz)
    assert session.close_at.strftime("%H:%M") == expected_close
    assert tuple(
        (start.strftime("%H:%M"), end.strftime("%H:%M"))
        for start, end in session.segments
    ) == expected_segments


def test_default_calendar_resolver_delegates_to_existing_trading_calendar(monkeypatch):
    calls = []
    session_date = date(2026, 1, 5)

    def fake_is_market_open(market, check_date):
        calls.append((market, check_date))
        return check_date == session_date

    monkeypatch.setattr(trading_calendar, "is_market_open", fake_is_market_open)
    clock = MarketClock("H")

    assert clock.is_trading_day(
        datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    )
    assert calls == [("hk", session_date)]


def test_holiday_is_non_trading_and_cutoff_uses_previous_session():
    clock = MarketClock(
        "A",
        trading_day_resolver=_fixed_calendar(date(2026, 1, 2)),
    )
    holiday_noon = datetime(
        2026, 1, 5, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    assert not clock.is_trading_day(holiday_noon)
    assert clock.phase(holiday_noon) is MarketPhase.NON_TRADING
    assert clock.observation_cutoff(holiday_noon) == datetime(
        2026,
        1,
        2,
        15,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )


def test_a_share_premarket_lunch_and_postmarket_boundaries():
    session_date = date(2026, 1, 5)
    clock = MarketClock("cn", trading_day_resolver=_fixed_calendar(session_date))
    tz = ZoneInfo("Asia/Shanghai")

    cases = [
        (datetime(2026, 1, 5, 9, 29, tzinfo=tz), MarketPhase.PREMARKET),
        (datetime(2026, 1, 5, 9, 30, tzinfo=tz), MarketPhase.INTRADAY),
        (datetime(2026, 1, 5, 11, 30, tzinfo=tz), MarketPhase.LUNCH_BREAK),
        (datetime(2026, 1, 5, 12, 30, tzinfo=tz), MarketPhase.LUNCH_BREAK),
        (datetime(2026, 1, 5, 13, 0, tzinfo=tz), MarketPhase.INTRADAY),
        (datetime(2026, 1, 5, 15, 0, tzinfo=tz), MarketPhase.POSTMARKET),
    ]

    for current_time, expected_phase in cases:
        assert clock.phase(current_time) is expected_phase

    assert clock.observation_cutoff(cases[2][0]) == datetime(
        2026, 1, 5, 11, 30, tzinfo=tz
    )


def test_us_utc_cross_midnight_uses_new_york_session_date():
    session_date = date(2026, 1, 5)
    clock = MarketClock("US", trading_day_resolver=_fixed_calendar(session_date))
    utc_time = datetime(2026, 1, 6, 0, 30, tzinfo=timezone.utc)

    assert clock.to_local(utc_time) == datetime(
        2026, 1, 5, 19, 30, tzinfo=ZoneInfo("America/New_York")
    )
    assert clock.is_trading_day(utc_time)
    assert clock.phase(utc_time) is MarketPhase.POSTMARKET
    assert clock.observation_cutoff(utc_time) == datetime(
        2026,
        1,
        5,
        16,
        0,
        tzinfo=ZoneInfo("America/New_York"),
    )


def test_us_session_cutoff_tracks_dst_without_using_machine_timezone():
    winter_date = date(2026, 1, 5)
    summer_date = date(2026, 7, 6)
    clock = MarketClock(
        "us",
        trading_day_resolver=_fixed_calendar(winter_date, summer_date),
    )

    winter = clock.session_for(winter_date)
    summer = clock.session_for(summer_date)

    assert winter.open_at.astimezone(timezone.utc) == datetime(
        2026, 1, 5, 14, 30, tzinfo=timezone.utc
    )
    assert summer.open_at.astimezone(timezone.utc) == datetime(
        2026, 7, 6, 13, 30, tzinfo=timezone.utc
    )


def test_naive_datetimes_are_rejected_instead_of_using_local_machine_timezone():
    clock = MarketClock("A", trading_day_resolver=_fixed_calendar(date(2026, 1, 5)))
    naive = datetime(2026, 1, 5, 10, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        clock.phase(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.is_stale(naive, now=naive)


def test_stale_threshold_is_strict_and_excludes_closed_market_time():
    session_one = date(2026, 1, 5)
    session_two = date(2026, 1, 6)
    clock = MarketClock(
        "A",
        stale_threshold=timedelta(minutes=10),
        trading_day_resolver=_fixed_calendar(session_one, session_two),
    )
    tz = ZoneInfo("Asia/Shanghai")

    now = datetime(2026, 1, 5, 10, 10, tzinfo=tz)
    exactly_at_threshold = datetime(2026, 1, 5, 10, 0, tzinfo=tz)
    one_second_over = datetime(2026, 1, 5, 9, 59, 59, tzinfo=tz)
    assert clock.stale_age(exactly_at_threshold, now=now) == timedelta(minutes=10)
    assert not clock.is_stale(exactly_at_threshold, now=now)
    assert clock.is_stale(one_second_over, now=now)

    lunch = datetime(2026, 1, 5, 12, 0, tzinfo=tz)
    lunch_close = datetime(2026, 1, 5, 11, 30, tzinfo=tz)
    assert clock.stale_age(lunch_close, now=lunch) == timedelta(0)
    assert not clock.is_stale(lunch_close, now=lunch)

    next_open = datetime(2026, 1, 6, 9, 41, tzinfo=tz)
    prior_close = datetime(2026, 1, 5, 15, 0, tzinfo=tz)
    assert clock.is_stale(prior_close, now=next_open)


def test_stale_age_accepts_utc_as_of_for_a_local_market():
    session_date = date(2026, 1, 5)
    clock = MarketClock("H", trading_day_resolver=_fixed_calendar(session_date))
    as_of_utc = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)
    now_hk = datetime(2026, 1, 5, 10, 10, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    assert clock.stale_age(as_of_utc, now=now_hk) == timedelta(minutes=10)
