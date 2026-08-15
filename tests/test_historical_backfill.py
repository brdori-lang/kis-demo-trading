from datetime import date, datetime, timedelta, timezone

import pytest

from lab_repository import SQLiteLabRepository
from market_data import HistoricalBackfillError, HistoricalMarketDataService


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def item(day, close=100):
    text = day.strftime("%Y%m%d") if hasattr(day, "strftime") else day
    return {
        "trade_date": text,
        "open_price": close,
        "high_price": close + 1,
        "low_price": close - 1,
        "close_price": close,
        "volume": 1_000,
        "trading_value": 100_000,
    }


def response(days):
    return {
        "rt_cd": "0",
        "output2": [
            {
                "stck_bsop_date": row["trade_date"],
                "stck_oprc": str(row["open_price"]),
                "stck_hgpr": str(row["high_price"]),
                "stck_lwpr": str(row["low_price"]),
                "stck_clpr": str(row["close_price"]),
                "acml_vol": str(row["volume"]),
                "acml_tr_pbmn": str(row["trading_value"]),
            }
            for row in days
        ],
    }


def daily_range(start, end):
    result = []
    current = start
    while current <= end:
        result.append(item(current))
        current += timedelta(days=1)
    return result


def seed(repository, days):
    repository.upsert_daily_prices("005930", days, NOW.isoformat())


def paged_fetcher(all_days, calls):
    def fetch(stock_code, start_date, end_date):
        calls.append((stock_code, start_date, end_date))
        selected = [
            row
            for row in all_days
            if start_date <= row["trade_date"] <= end_date
        ]
        return response(list(reversed(selected[-100:])))

    return fetch


def test_backfills_backward_in_pages_and_rerun_makes_zero_calls(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "backfill.db")
    existing = daily_range(date(2025, 9, 10), date(2026, 1, 1))
    historical = daily_range(date(2025, 1, 1), date(2025, 9, 9))
    seed(repository, existing)
    calls = []
    sleeps = []
    service = HistoricalMarketDataService(
        repository,
        fetcher=paged_fetcher(historical, calls),
        now_provider=lambda: NOW,
        request_interval=0.6,
        sleep_func=sleeps.append,
    )

    result = service.backfill("005930", years=1, target_end_date="20260101")

    assert result["api_call_count"] == 3
    assert result["backfilled_count"] == len(historical)
    assert result["oldest_trade_date"] == "20250101"
    assert result["latest_trade_date"] == "20260101"
    assert calls[0][2] == "20250909"
    assert calls[1][2] == (datetime.strptime(min(row["trade_date"] for row in historical[-100:]), "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    assert sleeps == [0.6, 0.6]
    rows = repository.get_daily_prices("005930", 1_000)
    assert len(rows) == len({row["trade_date"] for row in rows})

    no_call_service = HistoricalMarketDataService(
        SQLiteLabRepository(tmp_path / "backfill.db"),
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("unexpected KIS call")),
        now_provider=lambda: NOW,
    )
    rerun = no_call_service.backfill("005930", years=1, target_end_date="20260101")
    assert rerun["api_call_count"] == 0
    assert rerun["backfilled_count"] == 0


def test_short_page_terminates_only_after_cursor_validation(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "short.db")
    existing = [item(date(2026, 1, 1))]
    seed(repository, existing)
    calls = []
    historical = daily_range(date(2025, 10, 1), date(2025, 12, 31))

    result = HistoricalMarketDataService(
        repository,
        fetcher=paged_fetcher(historical, calls),
        now_provider=lambda: NOW,
        request_interval=0,
    ).backfill("005930", years=1, target_end_date="20260101")

    assert len(calls) == 1
    assert result["backfilled_count"] == len(historical)


def test_backfill_does_not_change_latest_price_freshness_timestamp(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "freshness.db")
    latest_fetched_at = "2026-01-01T09:00:00+09:00"
    repository.upsert_daily_prices(
        "005930", [item(date(2026, 1, 1))], latest_fetched_at
    )
    service = HistoricalMarketDataService(
        repository,
        fetcher=lambda *_: response([item(date(2025, 12, 31))]),
        now_provider=lambda: NOW + timedelta(hours=2),
        request_interval=0,
    )

    service.backfill("005930", years=1, target_end_date="20260101")

    cache_info = repository.get_daily_price_cache_info("005930")
    assert cache_info["latest_trade_date"] == "20260101"
    assert cache_info["last_fetched_at"] == latest_fetched_at


def test_middle_page_failure_preserves_existing_database(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "failure.db")
    existing = [item(date(2026, 1, 1), 777)]
    seed(repository, existing)
    page = list(reversed(daily_range(date(2025, 9, 23), date(2025, 12, 31))))
    calls = 0

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(page)
        raise RuntimeError("network failure")

    service = HistoricalMarketDataService(
        repository, fetcher=fetch, now_provider=lambda: NOW, request_interval=0
    )
    with pytest.raises(HistoricalBackfillError):
        service.backfill("005930", years=1, target_end_date="20260101")

    rows = repository.get_daily_prices("005930", 100)
    assert len(rows) == 1
    assert rows[0]["close_price"] == 777


def test_database_failure_rolls_back_entire_upsert(tmp_path):
    class FailingRepository(SQLiteLabRepository):
        def upsert_daily_prices(self, stock_code, items, fetched_at):
            with self._connect() as connection:
                row = items[0]
                connection.execute(
                    """INSERT INTO daily_prices
                    (stock_code, trade_date, open_price, high_price, low_price,
                     close_price, volume, trading_value, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        stock_code, row["trade_date"], row["open_price"], row["high_price"],
                        row["low_price"], row["close_price"], row["volume"],
                        row["trading_value"], fetched_at,
                    ),
                )
                raise RuntimeError("simulated DB failure")

    repository = FailingRepository(tmp_path / "rollback.db")
    service = HistoricalMarketDataService(
        repository,
        fetcher=lambda *_: response([item(date(2025, 12, 31))]),
        now_provider=lambda: NOW,
        request_interval=0,
    )

    with pytest.raises(RuntimeError, match="simulated DB failure"):
        service.backfill("005930", years=1, target_end_date="20260101")
    assert repository.get_daily_price_range_info("005930")["item_count"] == 0


def test_repeated_page_and_nonmoving_cursor_are_rejected(tmp_path):
    page = list(reversed(daily_range(date(2025, 9, 23), date(2025, 12, 31))))
    repository = SQLiteLabRepository(tmp_path / "repeat.db")
    service = HistoricalMarketDataService(
        repository,
        fetcher=lambda *_: response(page),
        now_provider=lambda: NOW,
        request_interval=0,
    )
    with pytest.raises(HistoricalBackfillError, match="repeated"):
        service.backfill("005930", years=1, target_end_date="20260101")
    assert repository.get_daily_price_range_info("005930")["item_count"] == 0

    future_page = [item(date(2026, 1, 2))]
    with pytest.raises(HistoricalBackfillError, match="cursor did not move"):
        HistoricalMarketDataService(
            repository,
            fetcher=lambda *_: response(future_page),
            now_provider=lambda: NOW,
            request_interval=0,
        ).backfill("005930", years=1, target_end_date="20260101")


def test_max_calls_aborts_without_writing_partial_pages(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "max.db")
    historical = daily_range(date(2025, 1, 1), date(2025, 12, 31))
    calls = []
    service = HistoricalMarketDataService(
        repository,
        fetcher=paged_fetcher(historical, calls),
        now_provider=lambda: NOW,
        request_interval=0,
        max_calls=2,
    )

    with pytest.raises(HistoricalBackfillError, match="max_calls"):
        service.backfill("005930", years=1, target_end_date="20260101")
    assert len(calls) == 2
    assert repository.get_daily_price_range_info("005930")["item_count"] == 0


def test_egw00201_has_bounded_retry_and_injected_sleep(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "retry.db")
    calls = 0
    sleeps = []

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            return {"rt_cd": "1", "msg_cd": "EGW00201", "output2": []}
        return response([item(date(2025, 12, 31))])

    result = HistoricalMarketDataService(
        repository,
        fetcher=fetch,
        now_provider=lambda: NOW,
        request_interval=0.7,
        sleep_func=sleeps.append,
        max_rate_limit_retries=2,
    ).backfill("005930", years=1, target_end_date="20260101")

    assert result["api_call_count"] == 3
    assert sleeps == [0.7, 0.7]

    always_limited = HistoricalMarketDataService(
        SQLiteLabRepository(tmp_path / "retry-fail.db"),
        fetcher=lambda *_: {"rt_cd": "1", "msg_cd": "EGW00201", "output2": []},
        now_provider=lambda: NOW,
        request_interval=0,
        max_rate_limit_retries=1,
    )
    with pytest.raises(HistoricalBackfillError, match="retry was exhausted"):
        always_limited.backfill("005930", years=1, target_end_date="20260101")


def test_retry_after_is_used_only_when_exception_provides_it(tmp_path):
    class FakeResponse:
        headers = {"Retry-After": "2.5"}

        def json(self):
            return {"msg_cd": "EGW00201"}

    class RateLimitError(Exception):
        def __init__(self):
            self.response = FakeResponse()

    repository = SQLiteLabRepository(tmp_path / "header.db")
    calls = 0
    sleeps = []

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitError()
        return response([item(date(2025, 12, 31))])

    HistoricalMarketDataService(
        repository,
        fetcher=fetch,
        now_provider=lambda: NOW,
        request_interval=0.6,
        sleep_func=sleeps.append,
    ).backfill("005930", years=1, target_end_date="20260101")

    assert sleeps == [2.5]
