from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from edgeio_contracts.samples import sample_report
from edgeio_worker.migrate import apply_migrations, statements
from edgeio_worker.store import process_report


def test_tables_exist(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {"devices", "health_readings", "service_status", "alerts", "schema_migrations"} <= names


def test_health_readings_is_a_hypertable(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        "SELECT hypertable_name FROM timescaledb_information.hypertables"
    ).fetchall()
    assert ("health_readings",) in rows


def test_continuous_aggregates_exist(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        "SELECT view_name FROM timescaledb_information.continuous_aggregates"
    ).fetchall()
    assert {r[0] for r in rows} == {"health_hourly", "health_daily"}


def test_reapplying_is_a_no_op(database_url: str, migrations_dir: Path) -> None:
    assert apply_migrations(database_url, migrations_dir) == []


def test_only_one_open_alert_per_device_and_rule(conn: psycopg.Connection) -> None:
    now = datetime(2026, 10, 6, tzinfo=UTC)
    insert = (
        "INSERT INTO alerts (device_id, rule, severity, opened_at, resolved_at, message) "
        "VALUES ('100.64.0.1', 'cpu_temp_high', 'warning', %s, %s, 'm')"
    )
    conn.execute(insert, (now, now))  # resolved
    conn.execute(insert, (now, None))  # open
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (now, None))  # second open


def test_no_transaction_migrations_run_outside_a_transaction(
    database_url: str, migrations_dir: Path, tmp_path: Path, conn: psycopg.Connection
) -> None:
    for path in migrations_dir.glob("*.sql"):
        (tmp_path / path.name).write_text(path.read_text())
    (tmp_path / "9999_refresh.sql").write_text(
        "-- migrate: no-transaction\n"
        "CALL refresh_continuous_aggregate('health_hourly', NULL, NULL);\n"
        "CALL refresh_continuous_aggregate('health_daily', NULL, NULL);\n"
    )
    try:
        assert apply_migrations(database_url, tmp_path) == ["9999_refresh.sql"]
    finally:
        conn.execute("DELETE FROM schema_migrations WHERE version = '9999_refresh.sql'")


def test_rollup_backfill_restores_buckets_older_than_the_policy_window(
    conn: psycopg.Connection, migrations_dir: Path
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    five_hours_ago = now - timedelta(hours=5)
    process_report(conn, sample_report({"timestamp": five_hours_ago.isoformat()}))
    # Live data inside the policy window. Without it the refresh materializes nothing, the
    # watermark stays at -infinity, and real-time aggregation would mask the lost history.
    two_hours_ago = (now - timedelta(hours=2)).isoformat()
    process_report(conn, sample_report({"timestamp": two_hours_ago}))
    # What the hourly policy does on schedule: refresh only [now-3h, now-1h].
    conn.execute(
        "CALL refresh_continuous_aggregate('health_hourly', "
        "now() - INTERVAL '3 hours', now() - INTERVAL '1 hour')"
    )
    bucket = (
        "SELECT count(*) FROM health_hourly WHERE bucket = time_bucket('1 hour', %s::timestamptz)"
    )
    assert conn.execute(bucket, (five_hours_ago,)).fetchone() == (0,)  # history lost

    backfill = (migrations_dir / "0004_backfill_rollups.sql").read_text()
    for statement in statements(backfill):
        conn.execute(statement)
    assert conn.execute(bucket, (five_hours_ago,)).fetchone() == (1,)
