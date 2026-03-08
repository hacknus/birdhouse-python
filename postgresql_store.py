import datetime
import re
import threading
from typing import Any, Mapping

import psycopg


class PostgresTimeSeriesStore:
    _IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)

    def __init__(self, env_values: Mapping[str, Any]):
        self.table = str(env_values.get("POSTGRES_TABLE") or "influx_points")
        if not self._IDENTIFIER_RE.match(self.table):
            raise ValueError(
                f"Invalid POSTGRES_TABLE {self.table!r}. Use a simple SQL identifier."
            )

        self.bucket = str(
            env_values.get("POSTGRES_BUCKET")
            or env_values.get("INFLUXDB_BUCKET")
            or "voegeli"
        )
        self.dsn = self._build_dsn(env_values)
        self.auto_create = self._parse_bool(env_values.get("POSTGRES_AUTO_CREATE"), default=True)
        self.connect_timeout_s = int(env_values.get("POSTGRES_CONNECT_TIMEOUT") or 5)

        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()

        self._insert_sql = f"""
            INSERT INTO {self.table}
            (bucket, ts, measurement, field, value_double, value_bool, value_text, unit, location, type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

    @staticmethod
    def _parse_bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _build_dsn(env_values: Mapping[str, Any]) -> str:
        dsn = env_values.get("POSTGRES_DSN")
        if dsn:
            return str(dsn)

        host = env_values.get("POSTGRES_HOST")
        dbname = env_values.get("POSTGRES_DB")
        user = env_values.get("POSTGRES_USER")
        password = env_values.get("POSTGRES_PASSWORD")
        port = env_values.get("POSTGRES_PORT") or 5432

        if host and dbname and user and password:
            return f"host={host} port={port} dbname={dbname} user={user} password={password}"

        raise ValueError(
            "Set POSTGRES_DSN or all of POSTGRES_HOST/POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD."
        )

    def _ensure_connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                autocommit=True,
                connect_timeout=self.connect_timeout_s,
            )
            if self.auto_create:
                self._initialize_schema()
        return self._conn

    def _initialize_schema(self) -> None:
        assert self._conn is not None
        ts_idx = f"{self.table}_ts_idx"
        mf_idx = f"{self.table}_measurement_field_ts_idx"
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id BIGSERIAL PRIMARY KEY,
                    bucket TEXT NOT NULL,
                    ts TIMESTAMPTZ NOT NULL,
                    measurement TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value_double DOUBLE PRECISION,
                    value_bool BOOLEAN,
                    value_text TEXT,
                    unit TEXT,
                    location TEXT,
                    type TEXT,
                    CHECK (num_nonnulls(value_double, value_bool, value_text) = 1)
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {ts_idx} ON {self.table} (ts DESC)"
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {mf_idx}
                ON {self.table} (measurement, field, ts DESC)
                """
            )

    @staticmethod
    def _split_value(value: Any) -> tuple[float | None, bool | None, str | None]:
        if isinstance(value, bool):
            return None, value, None
        if isinstance(value, (int, float)):
            return float(value), None, None
        return None, None, str(value)

    @classmethod
    def _parse_duration(cls, duration: str) -> datetime.timedelta:
        match = cls._DURATION_RE.match(duration)
        if not match:
            raise ValueError(
                f"Unsupported duration format: {duration!r}. Use e.g. 10s, 1m, 2h, 7d."
            )

        amount = int(match.group(1))
        unit = match.group(2).lower()
        unit_map = {
            "s": "seconds",
            "m": "minutes",
            "h": "hours",
            "d": "days",
            "w": "weeks",
        }
        return datetime.timedelta(**{unit_map[unit]: amount})

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    def write_device_data(self, device_data: dict[str, Any], measurement: str | None = None) -> None:
        measurement_value = measurement or str(device_data.get("device"))
        if not measurement_value:
            raise ValueError("device_data must contain a non-empty 'device' value.")

        data = device_data.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("device_data['data'] must be a dictionary.")

        rows: list[tuple[Any, ...]] = []
        timestamp = datetime.datetime.now(datetime.timezone.utc)

        for field, value in data.items():
            if (
                field.endswith("_unit")
                or field.endswith("_location")
                or field.endswith("_type")
            ):
                continue
            if value is None:
                continue

            value_double, value_bool, value_text = self._split_value(value)
            rows.append(
                (
                    self.bucket,
                    timestamp,
                    measurement_value,
                    str(field),
                    value_double,
                    value_bool,
                    value_text,
                    data.get(f"{field}_unit"),
                    data.get(f"{field}_location"),
                    data.get(f"{field}_type"),
                )
            )

        if not rows:
            return

        with self._lock:
            conn = self._ensure_connection()
            with conn.cursor() as cur:
                cur.executemany(self._insert_sql, rows)

    def query_last(
        self,
        data_since: str = "1m",
        bucket: str | None = None,
        field: str = "heating_set_temperature",
        unit: str | None = "Kelvin",
    ) -> float | bool | str | None:
        since = datetime.datetime.now(datetime.timezone.utc) - self._parse_duration(data_since)
        query_bucket = bucket or self.bucket

        sql = f"""
            SELECT value_double, value_bool, value_text
            FROM {self.table}
            WHERE bucket = %s
              AND field = %s
              AND ts >= %s
        """
        params: list[Any] = [query_bucket, field, since]
        if unit is not None:
            sql += " AND unit = %s"
            params.append(unit)
        sql += " ORDER BY ts DESC LIMIT 1"

        with self._lock:
            conn = self._ensure_connection()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()

        if row is None:
            return None
        value_double, value_bool, value_text = row
        if value_double is not None:
            return value_double
        if value_bool is not None:
            return value_bool
        return value_text
