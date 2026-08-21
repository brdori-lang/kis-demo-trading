from datetime import date, timedelta

import pytest

from lab_repository import SQLiteLabRepository
from operations import OperationsService
from trading_lab import LabStore


def bars(count=60):
    start = date(2025, 1, 1)
    return [
        {
            "trade_date": (start + timedelta(days=index)).strftime("%Y%m%d"),
            "open_price": 100 + index,
            "high_price": 103 + index,
            "low_price": 98 + index,
            "close_price": 101 + index,
            "volume": 1_000 + index * 20,
            "trading_value": (101 + index) * (1_000 + index * 20),
        }
        for index in range(count)
    ]


@pytest.fixture
def operations(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "operations.db")
    repository.replace_stock_master(
        [{"stock_code": "005930", "stock_name": "삼성전자", "market": "KOSPI"}]
    )
    repository.upsert_daily_prices("005930", bars(), "2025-03-01T00:00:00Z")
    store = LabStore(repository)
    store.add_watch("005930", "삼성전자")
    return OperationsService(repository), store


def test_settings_are_persistent_and_real_trading_is_rejected(operations):
    service, store = operations

    saved = service.save_settings({"automation_enabled": True, "max_positions": 3})

    assert saved["automation_enabled"] is True
    assert saved["max_positions"] == 3
    assert OperationsService(store.repository).get_settings()["max_positions"] == 3
    with pytest.raises(ValueError, match="모의 실행"):
        service.save_settings({"dry_run": False})


def test_analysis_and_backtest_jobs_persist_results_and_logs(operations):
    service, store = operations

    analysis = service.run_job("analyze_watchlist", store)
    backtest = service.run_job("backtest_watchlist", store)

    assert analysis["status"] == "success"
    assert analysis["items"][0]["stock_code"] == "005930"
    assert service.list_signals()[0]["stock_name"] == "삼성전자"
    assert backtest["items"][0]["initial_cash"] == 10_000_000
    assert service.list_backtests()[0]["stock_code"] == "005930"
    assert sum(job["latest_run"] is not None for job in service.list_jobs()) == 2
    assert len(service.list_logs()) == 2


def test_auto_trade_job_only_creates_dry_run_candidates(operations):
    service, store = operations
    service.run_job("analyze_watchlist", store)

    result = service.run_job("auto_trade_dry_run", store)

    assert result["status"] == "success"
    assert all(item["mode"] == "dry_run" for item in result["items"])
    assert store.list_orders() == []


def test_automation_universe_can_disable_a_watchlist_stock(operations):
    service, store = operations

    updated = service.update_universe(store, "005930", {"enabled": False, "target_weight": 25})

    assert updated["enabled"] is False
    assert updated["target_weight"] == 25
    assert service.list_universe(store, enabled_only=True) == []


def test_collection_job_records_per_stock_result(monkeypatch, operations):
    service, store = operations

    class FakeMarketDataService:
        def __init__(self, repository):
            self.repository = repository

        def get_daily_prices(self, stock_code, days, refresh):
            assert refresh is True
            return {"items": bars(10), "as_of_date": "20250110"}

    monkeypatch.setattr("operations.DailyMarketDataService", FakeMarketDataService)

    result = service.run_job("collect_watchlist_data", store)

    assert result["status"] == "success"
    assert result["items"] == [
        {"stock_code": "005930", "status": "success", "data_points": 10, "as_of_date": "20250110"}
    ]


def test_job_run_history_returns_latest_first(operations):
    service, store = operations
    service.run_job("analyze_watchlist", store)
    service.run_job("backtest_watchlist", store)

    runs = service.list_job_runs()

    assert [run["job_name"] for run in runs] == ["backtest_watchlist", "analyze_watchlist"]
