"""Timezone-aware market sessions, observation cutoffs, and freshness.

This module is intentionally limited to clock semantics.  It does not define
data envelopes, source policy, or provider routing; those are later Market
Data tasks.  Trading-day decisions use the existing ``src.core.trading_calendar``
resolver by default and can be injected in deterministic tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, Optional, Tuple, Union
from zoneinfo import ZoneInfo


TradingDayResolver = Callable[[str, date], bool]

DEFAULT_STALE_THRESHOLD = timedelta(minutes=10)
_MAX_PREVIOUS_SESSION_LOOKBACK_DAYS = 3660


# Canonical market keys intentionally match SecurityId and the existing
# trading_calendar module.  A/H are accepted as user-facing aliases.
MARKET_ALIASES = {
    "a": "cn",
    "a-share": "cn",
    "a_share": "cn",
    "cn": "cn",
    "h": "hk",
    "h-share": "hk",
    "h_share": "hk",
    "hk": "hk",
    "us": "us",
    "u.s.": "us",
    "usa": "us",
}

MARKET_TIMEZONES = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
}

# Regular-session intervals in local market time.  Lunch breaks are kept as
# separate intervals so cutoff and stale age never count closed minutes.
MARKET_SESSION_TIMES: Dict[str, Tuple[Tuple[time, time], ...]] = {
    "cn": (
        (time(9, 30), time(11, 30)),
        (time(13, 0), time(15, 0)),
    ),
    "hk": (
        (time(9, 30), time(12, 0)),
        (time(13, 0), time(16, 0)),
    ),
    "us": ((time(9, 30), time(16, 0)),),
}


class MarketClockError(ValueError):
    """Raised for an unsupported market or an invalid clock timestamp."""


class MarketPhase(str, Enum):
    """Clock phase for a regular trading session."""

    PREMARKET = "premarket"
    INTRADAY = "intraday"
    LUNCH_BREAK = "lunch_break"
    POSTMARKET = "postmarket"
    NON_TRADING = "non_trading"


@dataclass(frozen=True)
class MarketSession:
    """A regular session represented entirely by aware local timestamps."""

    market: str
    session_date: date
    timezone: str
    segments: Tuple[Tuple[datetime, datetime], ...]

    @property
    def open_at(self) -> datetime:
        return self.segments[0][0]

    @property
    def close_at(self) -> datetime:
        return self.segments[-1][1]

    @property
    def cutoff(self) -> datetime:
        """The regular-session close used as the postmarket cutoff."""

        return self.close_at

    @property
    def session_open(self) -> datetime:
        """Compatibility alias for callers that use exchange terminology."""

        return self.open_at

    @property
    def session_close(self) -> datetime:
        """Compatibility alias for callers that use exchange terminology."""

        return self.close_at

    def contains(self, moment: datetime) -> bool:
        """Return whether an aware timestamp falls in a regular segment."""

        if moment.tzinfo is None or moment.utcoffset() is None:
            raise MarketClockError("MarketClock requires timezone-aware datetime")
        local = moment.astimezone(self.open_at.tzinfo)
        return any(start <= local < end for start, end in self.segments)


# Kept as a short public alias for code that wants to inspect the configured
# regular sessions without depending on the internal dataclass.
MARKET_SESSIONS = MARKET_SESSION_TIMES


# The import is deliberately lazy.  Importing data_provider should not force
# optional pandas/exchange-calendars dependencies just to access this value
# object; the existing resolver is loaded only when the default path is used.
trading_calendar = None


def _default_trading_day_resolver(market: str, check_date: date) -> bool:
    global trading_calendar
    if trading_calendar is None:
        from src.core import trading_calendar as existing_calendar

        trading_calendar = existing_calendar
    return bool(trading_calendar.is_market_open(market, check_date))


def _canonical_market(value: str) -> str:
    raw = getattr(value, "market", value)
    normalized = str(raw or "").strip().lower()
    market = MARKET_ALIASES.get(normalized)
    if market is None:
        raise MarketClockError(
            f"unsupported market: {value!r}; expected one of A, H, US"
        )
    return market


def _coerce_threshold(value: Union[timedelta, int, float]) -> timedelta:
    if isinstance(value, bool):
        raise MarketClockError("stale threshold must be a non-negative duration")
    if isinstance(value, timedelta):
        threshold = value
    elif isinstance(value, (int, float)):
        threshold = timedelta(seconds=float(value))
    else:
        raise MarketClockError("stale threshold must be timedelta or seconds")
    if threshold.total_seconds() < 0:
        raise MarketClockError("stale threshold must be non-negative")
    return threshold


class MarketClock:
    """Resolve market-local sessions and market-aware data freshness.

    ``now`` and ``as_of`` timestamps must be timezone-aware.  They are
    converted to the market timezone before a date, session, or cutoff is
    derived.  Freshness counts only elapsed regular-session minutes: overnight,
    lunch breaks, weekends, and exchange holidays do not make a quote stale by
    wall-clock duration alone.
    """

    def __init__(
        self,
        market: str,
        *,
        trading_day_resolver: Optional[TradingDayResolver] = None,
        stale_threshold: Union[timedelta, int, float] = DEFAULT_STALE_THRESHOLD,
        stale_threshold_seconds: Optional[Union[int, float]] = None,
    ) -> None:
        self.market = _canonical_market(market)
        self.timezone_name = MARKET_TIMEZONES[self.market]
        self.timezone = ZoneInfo(self.timezone_name)
        self._trading_day_resolver = (
            trading_day_resolver or _default_trading_day_resolver
        )
        if stale_threshold_seconds is not None:
            if stale_threshold != DEFAULT_STALE_THRESHOLD:
                raise MarketClockError(
                    "provide stale_threshold or stale_threshold_seconds, not both"
                )
            stale_threshold = stale_threshold_seconds
        self._stale_threshold = _coerce_threshold(stale_threshold)
        self._trading_day_cache: Dict[date, bool] = {}
        self._session_cache: Dict[date, Optional[MarketSession]] = {}

    @property
    def stale_threshold(self) -> timedelta:
        return self._stale_threshold

    @property
    def stale_threshold_seconds(self) -> float:
        return self._stale_threshold.total_seconds()

    def now(self, value: Optional[datetime] = None) -> datetime:
        """Return *value* in market local time, or the current aware time."""

        if value is None:
            return datetime.now(timezone.utc).astimezone(self.timezone)
        return self.to_local(value)

    def to_local(self, value: datetime) -> datetime:
        """Convert an aware timestamp to this market's timezone."""

        if not isinstance(value, datetime):
            raise MarketClockError("MarketClock expects a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise MarketClockError("MarketClock requires timezone-aware datetime")
        return value.astimezone(self.timezone)

    def is_trading_day(self, value: Union[date, datetime]) -> bool:
        """Return whether the market-local date is an exchange session."""

        check_date = self._local_date(value)
        return self._is_trading_date(check_date)

    def session_for(self, value: Union[date, datetime]) -> Optional[MarketSession]:
        """Return the regular session for a market-local date, if any."""

        session_date = self._local_date(value)
        if session_date in self._session_cache:
            return self._session_cache[session_date]
        if not self._is_trading_date(session_date):
            self._session_cache[session_date] = None
            return None

        segments = tuple(
            (
                datetime.combine(session_date, start, tzinfo=self.timezone),
                datetime.combine(session_date, end, tzinfo=self.timezone),
            )
            for start, end in MARKET_SESSION_TIMES[self.market]
        )
        session = MarketSession(
            market=self.market,
            session_date=session_date,
            timezone=self.timezone_name,
            segments=segments,
        )
        self._session_cache[session_date] = session
        return session

    def previous_session(self, value: Union[date, datetime]) -> Optional[MarketSession]:
        """Return the latest session strictly before the local date."""

        current_date = self._local_date(value)
        candidate = current_date - timedelta(days=1)
        for _ in range(_MAX_PREVIOUS_SESSION_LOOKBACK_DAYS):
            session = self.session_for(candidate)
            if session is not None:
                return session
            candidate -= timedelta(days=1)
        return None

    def phase(self, value: Optional[datetime] = None) -> MarketPhase:
        """Classify an aware timestamp relative to the regular session."""

        local = self.now(value)
        session = self.session_for(local.date())
        if session is None:
            return MarketPhase.NON_TRADING
        if local < session.open_at:
            return MarketPhase.PREMARKET
        for index, (start, end) in enumerate(session.segments):
            if start <= local < end:
                return MarketPhase.INTRADAY
            if index + 1 < len(session.segments):
                next_start = session.segments[index + 1][0]
                if end <= local < next_start:
                    return MarketPhase.LUNCH_BREAK
        return MarketPhase.POSTMARKET

    def is_open(self, value: Optional[datetime] = None) -> bool:
        """Return whether an aware timestamp is inside a regular segment."""

        local = self.now(value)
        session = self.session_for(local.date())
        return session is not None and session.contains(local)

    # The name reads naturally at call sites that already use ``market_open``
    # for a current timestamp, while ``is_trading_day`` remains date-only.
    is_market_open = is_open

    def observation_cutoff(self, value: Optional[datetime] = None) -> Optional[datetime]:
        """Return the latest market-local timestamp usable at *value*.

        During a regular segment the cutoff is the supplied timestamp.  During
        lunch it is the previous segment close.  Before open, after close, and
        on non-trading dates it is the previous/current completed session close.
        """

        local = self.now(value)
        session = self.session_for(local.date())
        if session is None or local < session.open_at:
            previous = self.previous_session(local.date())
            return previous.close_at if previous is not None else None
        if local >= session.close_at:
            return session.close_at

        for index, (start, end) in enumerate(session.segments):
            if start <= local < end:
                return local
            if index + 1 < len(session.segments):
                next_start = session.segments[index + 1][0]
                if end <= local < next_start:
                    return end
        return session.close_at

    # Short aliases keep the clock useful for both R2.4 data consumers and
    # R6.1 observation builders without introducing another abstraction.
    cutoff = observation_cutoff
    data_cutoff = observation_cutoff

    def stale_age(
        self,
        as_of: datetime,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[timedelta]:
        """Return elapsed regular-session time between ``as_of`` and cutoff."""

        as_of_local = self.to_local(as_of)
        cutoff = self.observation_cutoff(now)
        if cutoff is None or cutoff <= as_of_local:
            return timedelta(0)
        return self._regular_session_elapsed(as_of_local, cutoff)

    def stale_seconds(
        self,
        as_of: datetime,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[int]:
        """Return market-aware stale age in whole seconds."""

        age = self.stale_age(as_of, now=now)
        return None if age is None else int(age.total_seconds())

    def is_stale(
        self,
        as_of: datetime,
        now: Optional[datetime] = None,
        threshold: Optional[Union[timedelta, int, float]] = None,
        *,
        stale_threshold: Optional[Union[timedelta, int, float]] = None,
    ) -> bool:
        """Return whether ``as_of`` is older than the configured threshold.

        The comparison is strict (``age > threshold``), matching the existing
        realtime quote TTL semantics in ``DataFetcherManager``.
        """

        if threshold is not None and stale_threshold is not None:
            raise MarketClockError("provide threshold or stale_threshold, not both")
        selected_threshold = (
            stale_threshold if stale_threshold is not None else threshold
        )
        threshold_delta = (
            self._stale_threshold
            if selected_threshold is None
            else _coerce_threshold(selected_threshold)
        )
        age = self.stale_age(as_of, now=now)
        return age is not None and age > threshold_delta

    def _local_date(self, value: Union[date, datetime]) -> date:
        if isinstance(value, datetime):
            return self.to_local(value).date()
        if isinstance(value, date):
            return value
        raise MarketClockError("MarketClock expects a date or datetime")

    def _is_trading_date(self, check_date: date) -> bool:
        if check_date not in self._trading_day_cache:
            self._trading_day_cache[check_date] = bool(
                self._trading_day_resolver(self.market, check_date)
            )
        return self._trading_day_cache[check_date]

    def _regular_session_elapsed(
        self,
        start: datetime,
        end: datetime,
    ) -> timedelta:
        if end <= start:
            return timedelta(0)

        total = timedelta(0)
        current_date = start.date()
        while current_date <= end.date():
            session = self.session_for(current_date)
            if session is not None:
                for segment_start, segment_end in session.segments:
                    overlap_start = max(start, segment_start)
                    overlap_end = min(end, segment_end)
                    if overlap_end > overlap_start:
                        total += overlap_end - overlap_start
            current_date += timedelta(days=1)
        return total


__all__ = [
    "DEFAULT_STALE_THRESHOLD",
    "MARKET_ALIASES",
    "MARKET_SESSIONS",
    "MARKET_SESSION_TIMES",
    "MARKET_TIMEZONES",
    "MarketClock",
    "MarketClockError",
    "MarketPhase",
    "MarketSession",
]
