import json
from datetime import datetime, timezone

from backtest_engine import run_backtest
from indicators import calculate_technical_indicators
from market_data import DailyMarketDataService
from strategy_engine import evaluate_lab_strategy_v1


JOB_DEFINITIONS = {
    "collect_watchlist_data": "자동매매 대상 일봉 데이터 수집",
    "analyze_watchlist": "관심종목 지표·퀀트 시그널 분석",
    "backtest_watchlist": "관심종목 전략 백테스트",
    "auto_trade_dry_run": "자동매매 주문 후보 모의 생성",
    "daily_pipeline": "일봉 수집·분석·백테스트 통합 파이프라인",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class OperationsService:
    """Persistent operations layer. It never sends a real brokerage order."""

    def __init__(self, repository):
        self.repository = repository
        self._create_tables()

    def _create_tables(self):
        with self.repository._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_universe (
                    stock_code TEXT PRIMARY KEY,
                    stock_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    strategy_name TEXT NOT NULL DEFAULT 'LAB Strategy v1',
                    target_weight REAL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quant_signals (
                    stock_code TEXT PRIMARY KEY,
                    stock_name TEXT NOT NULL,
                    as_of_date TEXT,
                    signal TEXT NOT NULL,
                    score REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    calculated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    initial_cash BIGINT NOT NULL,
                    final_equity REAL NOT NULL,
                    total_return REAL NOT NULL,
                    mdd REAL NOT NULL,
                    trade_count INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    processed_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def get_settings(self):
        defaults = {
            "automation_enabled": False,
            "dry_run": True,
            "max_positions": 5,
            "max_order_amount": 1_000_000,
            "stop_loss_rate": 5.0,
            "take_profit_rate": 10.0,
            "analysis_schedule": "0 18 * * 1-5",
        }
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT setting_key, setting_value FROM operation_settings"
            ).fetchall()
        for row in rows:
            defaults[row["setting_key"]] = json.loads(row["setting_value"])
        return defaults

    def save_settings(self, values: dict):
        allowed = set(self.get_settings())
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"지원하지 않는 설정입니다: {', '.join(sorted(unknown))}")
        if values.get("dry_run") is False:
            raise ValueError("현재 버전은 안전을 위해 모의 실행만 지원합니다.")
        now = _now()
        with self.repository._connect() as connection:
            connection.executemany(
                """
                INSERT INTO operation_settings(setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value, updated_at=excluded.updated_at
                """,
                [(key, json.dumps(value, ensure_ascii=False), now) for key, value in values.items()],
            )
        return self.get_settings()

    def run_job(self, job_name: str, store):
        if job_name not in JOB_DEFINITIONS:
            raise ValueError("등록되지 않은 배치 작업입니다.")
        started = _now()
        with self.repository._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO job_runs(job_name,status,started_at) VALUES (?, 'running', ?)",
                (job_name, started),
            )
            run_id = cursor.lastrowid
        try:
            if job_name == "collect_watchlist_data":
                result = self._collect_universe_data(store)
            elif job_name == "analyze_watchlist":
                result = self._analyze_watchlist(store)
            elif job_name == "backtest_watchlist":
                result = self._backtest_watchlist(store)
            elif job_name == "auto_trade_dry_run":
                result = self.auto_trade_candidates(store)
            else:
                collected = self._collect_universe_data(store)
                analyzed = self._analyze_watchlist(store)
                backtested = self._backtest_watchlist(store)
                result = [
                    {"stage": "collect", "processed_count": len(collected), "items": collected},
                    {"stage": "analyze", "processed_count": len(analyzed), "items": analyzed},
                    {"stage": "backtest", "processed_count": len(backtested), "items": backtested},
                ]
            count = len(result)
            failures = sum(item.get("status") == "failed" for item in result if isinstance(item, dict))
            status = "partial" if failures else "success"
            self._finish_job(run_id, status, count - failures, failures, f"{count - failures}건 성공, {failures}건 실패")
            level = "WARNING" if failures else "INFO"
            self.log(level, "batch", f"{JOB_DEFINITIONS[job_name]} 완료: {count - failures}건 성공, {failures}건 실패")
            return {"run_id": run_id, "job_name": job_name, "status": status, "items": result}
        except Exception as error:
            self._finish_job(run_id, "failed", 0, 1, str(error))
            self.log("ERROR", "batch", f"{JOB_DEFINITIONS[job_name]} 실패: {error}")
            raise

    def _finish_job(self, run_id, status, processed, errors, message):
        with self.repository._connect() as connection:
            connection.execute(
                """UPDATE job_runs SET status=?,finished_at=?,processed_count=?,
                   error_count=?,message=? WHERE id=?""",
                (status, _now(), processed, errors, message, run_id),
            )

    def _analyze_watchlist(self, store):
        results = []
        for stock in self.list_universe(store, enabled_only=True):
            bars = self.repository.get_daily_prices(stock["stock_code"], 100)
            if not bars:
                continue
            indicators = calculate_technical_indicators(bars)
            rows = [{**indicator, "close_price": bar["close_price"]} for bar, indicator in zip(bars, indicators)]
            evaluation = evaluate_lab_strategy_v1(rows)
            latest = indicators[-1]
            score = self._score(latest, evaluation["signal"])
            item = {
                "stock_code": stock["stock_code"], "stock_name": stock["stock_name"],
                "as_of_date": latest["trade_date"], "signal": evaluation["signal"],
                "score": score, "reasons": evaluation.get("reasons", []), "calculated_at": _now(),
            }
            self._save_signal(item)
            results.append(item)
        return results

    @staticmethod
    def _score(latest, signal):
        score = 50.0 + {"BUY": 25, "SELL": -25}.get(signal, 0)
        rsi = latest.get("rsi_14")
        volume = latest.get("volume_ratio")
        if rsi is not None:
            score += max(-10, min(10, (50 - rsi) / 3))
        if volume is not None:
            score += max(-5, min(10, (volume - 1) * 10))
        return round(max(0, min(100, score)), 2)

    def _save_signal(self, item):
        with self.repository._connect() as connection:
            connection.execute(
                """INSERT INTO quant_signals VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(stock_code) DO UPDATE SET stock_name=excluded.stock_name,
                   as_of_date=excluded.as_of_date,signal=excluded.signal,score=excluded.score,
                   reasons_json=excluded.reasons_json,calculated_at=excluded.calculated_at""",
                (item["stock_code"], item["stock_name"], item["as_of_date"], item["signal"],
                 item["score"], json.dumps(item["reasons"], ensure_ascii=False), item["calculated_at"]),
            )

    def list_signals(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM quant_signals ORDER BY score DESC").fetchall()
        return [{**dict(row), "reasons": json.loads(row["reasons_json"])} for row in rows]

    def _backtest_watchlist(self, store):
        results = []
        for stock in self.list_universe(store, enabled_only=True):
            bars = self.repository.get_daily_prices(stock["stock_code"], 1000)
            if not bars:
                continue
            result = run_backtest(bars)
            summary = {key: result[key] for key in ("initial_cash", "final_equity", "total_return", "mdd", "trade_count")}
            with self.repository._connect() as connection:
                cursor = connection.execute(
                    """INSERT INTO backtest_runs(stock_code,strategy_name,initial_cash,final_equity,
                       total_return,mdd,trade_count,result_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (stock["stock_code"], "LAB Strategy v1", summary["initial_cash"], summary["final_equity"],
                     summary["total_return"], summary["mdd"], summary["trade_count"],
                     json.dumps(result, ensure_ascii=False), _now()),
                )
            results.append({"id": cursor.lastrowid, "stock_name": stock["stock_name"], **summary})
        return results

    def list_backtests(self, limit=30):
        with self.repository._connect() as connection:
            rows = connection.execute(
                """SELECT id,stock_code,strategy_name,initial_cash,final_equity,total_return,
                   mdd,trade_count,created_at FROM backtest_runs ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def auto_trade_candidates(self, store):
        settings = self.get_settings()
        enabled = {item["stock_code"]: item for item in self.list_universe(store, enabled_only=True)}
        signals = [item for item in self.list_signals()
                   if item["signal"] in {"BUY", "SELL"} and item["stock_code"] in enabled]
        return [{**item, "mode": "dry_run", "max_order_amount": settings["max_order_amount"],
                 "target_weight": enabled[item["stock_code"]]["target_weight"]}
                for item in signals[: settings["max_positions"]]]

    def sync_universe(self, store):
        watches = store.list_watch()
        now = _now()
        with self.repository._connect() as connection:
            connection.executemany(
                """INSERT INTO automation_universe(stock_code,stock_name,updated_at)
                   VALUES (?,?,?) ON CONFLICT(stock_code) DO UPDATE SET stock_name=excluded.stock_name""",
                [(item["stock_code"], item["stock_name"], now) for item in watches],
            )
        return self._read_universe()

    def list_universe(self, store, enabled_only=False):
        self.sync_universe(store)
        return self._read_universe(enabled_only)

    def _read_universe(self, enabled_only=False):
        query = "SELECT * FROM automation_universe"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY stock_name,stock_code"
        with self.repository._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def update_universe(self, store, stock_code, values):
        self.sync_universe(store)
        allowed = {"enabled", "strategy_name", "target_weight"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("지원하지 않는 자동매매 대상 설정입니다.")
        if values.get("target_weight") is not None and not 0 < values["target_weight"] <= 100:
            raise ValueError("목표비중은 0보다 크고 100 이하여야 합니다.")
        current = next((x for x in self.list_universe(store) if x["stock_code"] == stock_code), None)
        if not current:
            raise ValueError("관심종목에 먼저 등록해야 합니다.")
        merged = {**current, **values}
        with self.repository._connect() as connection:
            connection.execute(
                """UPDATE automation_universe SET enabled=?,strategy_name=?,target_weight=?,updated_at=?
                   WHERE stock_code=?""",
                (int(merged["enabled"]), merged["strategy_name"], merged["target_weight"], _now(), stock_code),
            )
        return next(x for x in self.list_universe(store) if x["stock_code"] == stock_code)

    def _collect_universe_data(self, store):
        service = DailyMarketDataService(self.repository)
        results = []
        for stock in self.list_universe(store, enabled_only=True):
            try:
                data = service.get_daily_prices(stock["stock_code"], 100, refresh=True)
                items = data["items"]
                results.append({"stock_code": stock["stock_code"], "status": "success",
                                "data_points": len(items),
                                "as_of_date": items[-1]["trade_date"] if items else None})
            except Exception as error:
                results.append({"stock_code": stock["stock_code"], "status": "failed",
                                "data_points": 0, "message": str(error)})
        return results

    def data_status(self, store):
        self.sync_universe(store)
        with self.repository._connect() as connection:
            rows = connection.execute(
                """SELECT u.stock_code,u.stock_name,u.enabled,COUNT(p.trade_date) data_points,
                   MAX(p.trade_date) latest_trade_date,MAX(p.fetched_at) last_fetched_at
                   FROM automation_universe u LEFT JOIN daily_prices p ON p.stock_code=u.stock_code
                   GROUP BY u.stock_code,u.stock_name,u.enabled ORDER BY u.stock_name"""
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def list_jobs(self):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 50").fetchall()
        latest = {row["job_name"]: dict(row) for row in rows}
        return [{"job_name": name, "description": desc, "latest_run": latest.get(name)}
                for name, desc in JOB_DEFINITIONS.items()]

    def list_job_runs(self, limit=100):
        with self.repository._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def log(self, level, category, message):
        with self.repository._connect() as connection:
            connection.execute(
                "INSERT INTO system_logs(level,category,message,created_at) VALUES (?,?,?,?)",
                (level, category, message, _now()),
            )

    def list_logs(self, limit=100):
        with self.repository._connect() as connection:
            rows = connection.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def overview(self, store):
        from config import settings as app_settings
        from kis_virtual_orders import KISOrderStore

        signals = self.list_signals()
        backtests = self.list_backtests(100)
        orders = store.list_orders()
        reconciliation_runs = KISOrderStore(self.repository).reconciliation_runs(1)
        return {
            "watchlist_count": len(store.list_watch()),
            "signal_count": len(signals),
            "buy_signal_count": sum(item["signal"] == "BUY" for item in signals),
            "mock_order_count": len(orders),
            "backtest_count": len(backtests),
            "average_backtest_return": (sum(x["total_return"] for x in backtests) / len(backtests)) if backtests else None,
            "automation": self.get_settings(),
            "latest_jobs": self.list_jobs(),
            "data_status": self.data_status(store),
            "order_safety": {
                "environment": "KIS_VIRTUAL",
                "real_orders": "BLOCKED",
                "paper_order_enabled": app_settings.PAPER_ORDER_ENABLED,
                "vts_submit_enabled": app_settings.KIS_VIRTUAL_ORDER_SUBMIT_ENABLED,
            },
            "latest_reconciliation": reconciliation_runs[0] if reconciliation_runs else None,
        }
