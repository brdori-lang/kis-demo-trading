from datetime import datetime, timedelta, timezone
import time

import kis_api
from stock_master import normalize_stock_code


SEOUL = timezone(timedelta(hours=9))


def _number(value, field_name: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"KIS 일봉 응답의 {field_name} 값이 올바르지 않습니다.") from None


def normalize_daily_prices(response: dict):
    if response.get("rt_cd") not in (None, "0"):
        raise ValueError("KIS 일봉 조회에 실패했습니다.")
    output = response.get("output2")
    if not isinstance(output, list) or not output:
        raise ValueError("KIS 일봉 응답이 비어 있습니다.")

    items = []
    seen_dates = set()
    for row in output:
        trade_date = str(row.get("stck_bsop_date") or "")
        if len(trade_date) != 8 or not trade_date.isdigit():
            raise ValueError("KIS 일봉 응답의 거래일이 올바르지 않습니다.")
        if trade_date in seen_dates:
            continue
        seen_dates.add(trade_date)
        items.append(
            {
                "trade_date": trade_date,
                "open_price": _number(row.get("stck_oprc"), "시가"),
                "high_price": _number(row.get("stck_hgpr"), "고가"),
                "low_price": _number(row.get("stck_lwpr"), "저가"),
                "close_price": _number(row.get("stck_clpr"), "종가"),
                "volume": _number(row.get("acml_vol"), "거래량"),
                "trading_value": _number(row.get("acml_tr_pbmn") or 0, "거래대금"),
            }
        )
    return sorted(items, key=lambda item: item["trade_date"])


class DailyMarketDataService:
    def __init__(self, repository, fetcher=None, now_provider=None):
        self.repository = repository
        self.fetcher = fetcher
        self.now_provider = now_provider or (lambda: datetime.now(SEOUL))

    def _is_cache_fresh(self, stock_code: str, days: int, now: datetime):
        info = self.repository.get_daily_price_cache_info(stock_code)
        if info["item_count"] < days or not info["latest_trade_date"] or not info["last_fetched_at"]:
            return False
        latest_trade_date = datetime.strptime(info["latest_trade_date"], "%Y%m%d").date()
        fetched_at = datetime.fromisoformat(info["last_fetched_at"])
        return latest_trade_date >= now.date() - timedelta(days=10) and fetched_at.date() == now.date()

    def get_daily_prices(self, stock_code: str, days: int = 100, refresh: bool = False):
        normalized_code = normalize_stock_code(stock_code)
        if days < 1 or days > 100:
            raise ValueError("days는 1에서 100 사이여야 합니다.")
        now = self.now_provider()

        if not refresh and self._is_cache_fresh(normalized_code, days, now):
            return self._result(normalized_code, days, cached=True)

        start_date = (now.date() - timedelta(days=200)).strftime("%Y%m%d")
        end_date = now.date().strftime("%Y%m%d")
        fetcher = self.fetcher or kis_api.get_daily_item_chart_price
        response = fetcher(normalized_code, start_date, end_date)
        items = normalize_daily_prices(response)[-100:]
        fetched_at = now.isoformat(timespec="seconds")
        self.repository.upsert_daily_prices(normalized_code, items, fetched_at)
        return self._result(normalized_code, days, cached=False)

    def _result(self, stock_code: str, days: int, cached: bool):
        rows = self.repository.get_daily_prices(stock_code, days)
        items = [
            {key: value for key, value in row.items() if key != "fetched_at"}
            for row in rows
        ]
        return {
            "stock_code": stock_code,
            "period": "D",
            "adjusted": True,
            "cached": cached,
            "items": items,
        }


class HistoricalBackfillError(RuntimeError):
    pass


class HistoricalMarketDataService:
    def __init__(
        self,
        repository,
        fetcher=None,
        now_provider=None,
        request_interval=1.05,
        sleep_func=None,
        max_calls=15,
        max_rate_limit_retries=2,
    ):
        if request_interval < 0:
            raise ValueError("request_interval must not be negative")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        if max_rate_limit_retries < 0:
            raise ValueError("max_rate_limit_retries must not be negative")
        self.repository = repository
        self.fetcher = fetcher or kis_api.get_daily_item_chart_price
        self.now_provider = now_provider or (lambda: datetime.now(SEOUL))
        self.request_interval = request_interval
        self.sleep_func = sleep_func or time.sleep
        self.max_calls = max_calls
        self.max_rate_limit_retries = max_rate_limit_retries

    def backfill(self, stock_code: str, years: int = 3, target_end_date=None):
        normalized_code = normalize_stock_code(stock_code)
        if years < 1:
            raise ValueError("years must be positive")
        target_end = _as_date(target_end_date) if target_end_date else self.now_provider().date()
        target_start = _subtract_years(target_end, years)
        range_info = self.repository.get_daily_price_range_info(normalized_code)
        oldest_stored = range_info["oldest_trade_date"]

        if oldest_stored and oldest_stored <= target_start.strftime("%Y%m%d"):
            return self._result(normalized_code, target_start, target_end, 0, 0, True)

        cursor_end = (
            datetime.strptime(oldest_stored, "%Y%m%d").date() - timedelta(days=1)
            if oldest_stored
            else target_end
        )
        if cursor_end < target_start:
            return self._result(normalized_code, target_start, target_end, 0, 0, True)

        collected = {}
        seen_pages = set()
        call_count = 0
        complete = False

        while not complete:
            if call_count >= self.max_calls:
                raise HistoricalBackfillError("Historical backfill exceeded max_calls.")
            response, attempts = self._fetch_with_rate_limit_retry(
                normalized_code,
                target_start.strftime("%Y%m%d"),
                cursor_end.strftime("%Y%m%d"),
                self.max_calls - call_count,
                wait_before_first=call_count > 0,
            )
            call_count += attempts
            output = response.get("output2") if isinstance(response, dict) else None
            if response.get("rt_cd") not in (None, "0"):
                raise HistoricalBackfillError("KIS historical data request failed.")
            if not output:
                complete = True
                continue
            if not isinstance(output, list) or len(output) > 100:
                raise HistoricalBackfillError("KIS historical page has an invalid size.")

            page = normalize_daily_prices(response)
            page_dates = tuple(item["trade_date"] for item in page)
            page_key = frozenset(page_dates)
            if page_key in seen_pages:
                raise HistoricalBackfillError("KIS historical page was repeated.")
            seen_pages.add(page_key)
            oldest_page_date = datetime.strptime(page_dates[0], "%Y%m%d").date()
            next_cursor = oldest_page_date - timedelta(days=1)
            if next_cursor >= cursor_end:
                raise HistoricalBackfillError("KIS historical cursor did not move backward.")
            start_text = target_start.strftime("%Y%m%d")
            cursor_text = cursor_end.strftime("%Y%m%d")
            if any(date < start_text or date > cursor_text for date in page_dates):
                raise HistoricalBackfillError("KIS historical page is outside the requested range.")
            for item in page:
                collected[item["trade_date"]] = item

            if oldest_page_date <= target_start or len(output) < 100:
                complete = True
            else:
                cursor_end = next_cursor

        items = [collected[date] for date in sorted(collected)]
        if items:
            fetched_at = self.now_provider().isoformat(timespec="seconds")
            # A single repository call performs the complete upsert transaction.
            self.repository.upsert_daily_prices(normalized_code, items, fetched_at)
        return self._result(normalized_code, target_start, target_end, call_count, len(items), True)

    def _fetch_with_rate_limit_retry(
        self, stock_code, start_date, end_date, remaining_calls, wait_before_first
    ):
        attempts = 0
        retries = 0
        wait_seconds = self.request_interval if wait_before_first else 0
        while attempts < remaining_calls:
            if wait_seconds > 0:
                self.sleep_func(wait_seconds)
            attempts += 1
            try:
                response = self.fetcher(stock_code, start_date, end_date)
            except Exception as exc:
                if _exception_error_code(exc) != "EGW00201" or retries >= self.max_rate_limit_retries:
                    raise HistoricalBackfillError("KIS historical data request failed.") from None
                retries += 1
                wait_seconds = _exception_retry_after(exc)
                if wait_seconds is None:
                    wait_seconds = self.request_interval
                continue

            if _response_error_code(response) != "EGW00201":
                return response, attempts
            if retries >= self.max_rate_limit_retries:
                raise HistoricalBackfillError("KIS rate limit retry was exhausted.")
            retries += 1
            wait_seconds = self.request_interval
        raise HistoricalBackfillError("Historical backfill exceeded max_calls.")

    def _result(self, stock_code, target_start, target_end, api_call_count, inserted_count, complete):
        info = self.repository.get_daily_price_range_info(stock_code)
        return {
            "stock_code": stock_code,
            "target_start_date": target_start.strftime("%Y%m%d"),
            "target_end_date": target_end.strftime("%Y%m%d"),
            "api_call_count": api_call_count,
            "backfilled_count": inserted_count,
            "total_count": info["item_count"],
            "oldest_trade_date": info["oldest_trade_date"],
            "latest_trade_date": info["latest_trade_date"],
            "complete": complete,
        }


def _as_date(value):
    if hasattr(value, "date") and not isinstance(value, str):
        value = value.date() if isinstance(value, datetime) else value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            raise ValueError("target_end_date must use YYYYMMDD format") from None
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value
    raise ValueError("target_end_date must be a date or YYYYMMDD string")


def _subtract_years(value, years):
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _response_error_code(response):
    return response.get("msg_cd") if isinstance(response, dict) else None


def _exception_error_code(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    return _response_error_code(payload)


def _exception_retry_after(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
