from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DEFAULT_DB = Path(os.environ.get("FOLIOTRACKER_DB", "data/foliotracker.db"))


class Money(TypeDecorator):
    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


WRAPPERS = ("mf_folio", "demat", "nps", "epf", "ppf", "fd", "bank", "sgb", "other")
INSTRUMENT_KINDS = ("mf", "equity", "bond", "nps_scheme")
RTA_SOURCES = ("CAMS", "KFINTECH")


class ImportRun(Base):
    __tablename__ = "import_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    cas_type: Mapped[str | None] = mapped_column(String(16))
    period_from: Mapped[date] = mapped_column(Date)
    period_to: Mapped[date] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    statement_total: Mapped[Decimal | None] = mapped_column(Money)
    parsed_total: Mapped[Decimal] = mapped_column(Money)
    warnings: Mapped[str] = mapped_column(Text, default="[]")
    covers: Mapped[str | None] = mapped_column(String(64))

    positions: Mapped[list[Position]] = relationship(back_populates="import_run")

    @property
    def covered_kinds(self) -> set[str] | None:
        return set(self.covers.split(",")) if self.covers else None

    @property
    def is_rta(self) -> bool:
        return self.file_type in RTA_SOURCES


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (UniqueConstraint("wrapper", "identifier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wrapper: Mapped[str] = mapped_column(String(16))
    identifier: Mapped[str] = mapped_column(String(64))
    institution: Mapped[str | None] = mapped_column(String(255))
    holders: Mapped[str | None] = mapped_column(String(255))
    label: Mapped[str | None] = mapped_column(String(255))
    asset_class: Mapped[str | None] = mapped_column(String(16))
    manual: Mapped[bool] = mapped_column(default=False)
    statement_total: Mapped[Decimal | None] = mapped_column(Money)
    statement_total_as_of: Mapped[date | None] = mapped_column(Date)

    positions: Mapped[list[Position]] = relationship(back_populates="account")
    snapshots: Mapped[list[Snapshot]] = relationship(back_populates="account", cascade="all, delete-orphan")
    deposit: Mapped[Deposit | None] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        return self.label or f"{self.institution or self.wrapper} {self.identifier}"


class Instrument(Base):
    __tablename__ = "instrument"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    isin: Mapped[str | None] = mapped_column(String(12), index=True)
    amfi_code: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    symbol: Mapped[str | None] = mapped_column(String(32))
    amc: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(255))
    nps_tier: Mapped[str | None] = mapped_column(String(4))
    nps_asset_class: Mapped[str | None] = mapped_column(String(4))
    coupon: Mapped[Decimal | None] = mapped_column(Money)
    maturity_date: Mapped[str | None] = mapped_column(String(10))
    face_value: Mapped[Decimal | None] = mapped_column(Money)

    positions: Mapped[list[Position]] = relationship(back_populates="instrument")


class Position(Base):
    __tablename__ = "position"
    __table_args__ = (UniqueConstraint("import_run_id", "account_id", "instrument_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("import_run.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    as_of: Mapped[date] = mapped_column(Date, index=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal | None] = mapped_column(Money)
    value: Mapped[Decimal] = mapped_column(Money)
    cost: Mapped[Decimal | None] = mapped_column(Money)

    import_run: Mapped[ImportRun] = relationship(back_populates="positions")
    account: Mapped[Account] = relationship(back_populates="positions")
    instrument: Mapped[Instrument] = relationship(back_populates="positions")


class Transaction(Base):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    date: Mapped[date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    units: Mapped[Decimal | None] = mapped_column(Money)
    nav: Mapped[Decimal | None] = mapped_column(Money)
    balance: Mapped[Decimal | None] = mapped_column(Money)


class Valuation(Base):
    __tablename__ = "valuation"
    __table_args__ = (UniqueConstraint("instrument_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(16))


class Deposit(Base):
    __tablename__ = "deposit"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), unique=True)
    principal: Mapped[Decimal] = mapped_column(Money)
    rate: Mapped[Decimal] = mapped_column(Money)
    start_date: Mapped[date] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date)
    compounding: Mapped[str] = mapped_column(String(16))

    account: Mapped[Account] = relationship(back_populates="deposit")


class Snapshot(Base):
    __tablename__ = "snapshot"
    __table_args__ = (UniqueConstraint("account_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    date: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(String(255))

    account: Mapped[Account] = relationship(back_populates="snapshots")


class Allocation(Base):
    __tablename__ = "allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instrument.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), index=True)
    vector: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(255))


class KiteSession(Base):
    __tablename__ = "kite_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32))
    user_name: Mapped[str | None] = mapped_column(String(128))
    access_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NetWorthHistory(Base):
    __tablename__ = "net_worth_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    total: Mapped[Decimal] = mapped_column(Money)
    statement_total: Mapped[Decimal] = mapped_column(Money)
    by_class: Mapped[str] = mapped_column(Text)
    by_wrapper: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="live")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def migrate(path: Path | str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("foliotracker_db", str(path))
    command.upgrade(cfg, "head")


def make_engine(path: Path | str = DEFAULT_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrate(path)
    return create_engine(f"sqlite:///{path}")


def session_factory(path: Path | str = DEFAULT_DB) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(path), expire_on_commit=False)
