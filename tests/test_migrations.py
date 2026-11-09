from pathlib import Path

from sqlalchemy import create_engine, inspect

from foliotracker.db import Base, migrate


def test_migrations_build_the_same_schema_as_the_models(tmp_path: Path):
    db = tmp_path / "fresh.db"
    migrate(db)
    migrated = inspect(create_engine(f"sqlite:///{db}"))
    expected = create_engine("sqlite://")
    Base.metadata.create_all(expected)
    model_tables = set(inspect(expected).get_table_names())
    assert set(migrated.get_table_names()) - {"alembic_version"} == model_tables
    for table in model_tables:
        got = {c["name"] for c in migrated.get_columns(table)}
        want = {c["name"] for c in inspect(expected).get_columns(table)}
        assert got == want, table


def test_migrate_is_idempotent(tmp_path: Path):
    db = tmp_path / "twice.db"
    migrate(db)
    migrate(db)
    assert "alembic_version" in inspect(create_engine(f"sqlite:///{db}")).get_table_names()
