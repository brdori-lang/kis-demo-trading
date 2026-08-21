import re


class PostgresCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PostgresConnection:
    _serial_tables = {"mock_orders", "backtest_runs", "job_runs", "system_logs"}

    def __init__(self, database_url):
        import psycopg
        from psycopg.rows import dict_row

        self._connection = psycopg.connect(database_url, row_factory=dict_row)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    @staticmethod
    def _sql(sql):
        converted = sql.replace("?", "%s")
        converted = re.sub(r"\s+COLLATE\s+NOCASE", "", converted, flags=re.IGNORECASE)
        converted = re.sub(r"\browid\b", "stock_code", converted, flags=re.IGNORECASE)
        converted = re.sub(
            r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
            "BIGSERIAL PRIMARY KEY",
            converted,
            flags=re.IGNORECASE,
        )
        return converted

    def execute(self, sql, params=None):
        if "DELETE FROM sqlite_sequence" in sql:
            cursor = self._connection.execute("ALTER SEQUENCE mock_orders_id_seq RESTART WITH 1")
            return PostgresCursor(cursor)
        converted = self._sql(sql)
        match = re.match(r"\s*INSERT\s+INTO\s+([a-z_]+)", converted, re.IGNORECASE)
        wants_id = match and match.group(1).lower() in self._serial_tables and "RETURNING" not in converted.upper()
        if wants_id:
            converted = converted.rstrip().rstrip(";") + " RETURNING id"
        cursor = self._connection.execute(converted, params or ())
        lastrowid = cursor.fetchone()["id"] if wants_id else None
        return PostgresCursor(cursor, lastrowid)

    def executemany(self, sql, params_seq):
        cursor = self._connection.cursor()
        cursor.executemany(self._sql(sql), params_seq)
        return PostgresCursor(cursor)

    def executescript(self, sql):
        cursor = self._connection.execute(self._sql(sql))
        return PostgresCursor(cursor)
