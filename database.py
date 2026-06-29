"""
database.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime,
    Text, Index, JSON, update, delete, select, desc, func, event, text,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger("db")

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _is_postgres(url: str) -> bool:
    return "postgresql" in url or "postgres" in url

class Base(DeclarativeBase):
    pass

class Trade(Base):
    __tablename__ = "trades"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    timestamp         = Column(DateTime, nullable=False, default=_utcnow, index=True)
    order_id          = Column(String(64),  unique=True, nullable=False)
    symbol            = Column(String(32),  nullable=False, index=True)
    side              = Column(String(5),   nullable=False)
    order_type        = Column(String(12),  nullable=False)
    status            = Column(String(16),  nullable=False)

    requested_price   = Column(Float, nullable=True)
    executed_price    = Column(Float, nullable=True)
    amount            = Column(Float, nullable=False)
    filled            = Column(Float, nullable=True)
    cost              = Column(Float, nullable=True)

    fee_cost          = Column(Float, nullable=True)
    fee_currency      = Column(String(12), nullable=True)
    fee_rate          = Column(Float, nullable=True)
    slippage_pct      = Column(Float, nullable=True)

    stop_loss_price   = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)

    realized_pnl      = Column(Float, nullable=True)
    realized_pnl_pct  = Column(Float, nullable=True)

    strategy_name     = Column(String(64),  nullable=True, index=True)
    strategy_profile  = Column(String(64),  nullable=True, index=True) 
    signal_origin     = Column(String(500), nullable=True)
    notes             = Column(Text,        nullable=True)

    __table_args__ = (
        Index("ix_trades_symbol_ts",      "symbol", "timestamp"),
        Index("ix_trades_symbol_profile", "symbol", "strategy_profile"),
    )

class Position(Base):
    __tablename__ = "positions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    symbol              = Column(String(32), nullable=False, index=True)
    entry_time          = Column(DateTime,   nullable=False)
    exit_time           = Column(DateTime,   nullable=True)
    entry_price         = Column(Float,      nullable=False)
    current_price       = Column(Float,      nullable=True)
    amount              = Column(Float,      nullable=False)
    side                = Column(String(5),  nullable=False)
    is_open             = Column(Boolean,    default=True, index=True)
    is_closing          = Column(Boolean,    default=False, nullable=False)
    stop_loss_price     = Column(Float, nullable=True)
    take_profit_price   = Column(Float, nullable=True)
    atr_at_entry        = Column(Float, nullable=True)
    entry_fee_actual    = Column(Float, nullable=True)
    unrealized_pnl      = Column(Float, nullable=True)
    unrealized_pnl_pct  = Column(Float, nullable=True)
    realized_pnl        = Column(Float, nullable=True)
    realized_pnl_pct    = Column(Float, nullable=True)
    strategy_name       = Column(String(64), nullable=True)
    strategy_profile    = Column(String(64), nullable=True)  
    entry_order_id      = Column(String(64), nullable=True)
    entry_score         = Column(Float,      nullable=True)  
    entry_regime        = Column(String(32), nullable=True) 
    highest_price       = Column(Float,      nullable=True)

    __table_args__ = (
        Index("ix_positions_symbol_open", "symbol", "is_open"),
    )

class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    timestamp      = Column(DateTime, nullable=False, default=_utcnow, index=True)
    total_equity   = Column(Float, nullable=False)
    free_balance   = Column(Float, nullable=False)
    locked_balance = Column(Float, nullable=False)
    open_pnl       = Column(Float, nullable=True)
    daily_pnl      = Column(Float, nullable=True)
    daily_pnl_pct  = Column(Float, nullable=True)
    drawdown_pct   = Column(Float, nullable=True)

class OHLCVBar(Base):
    __tablename__ = "ohlcv"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(32), nullable=False, index=True)
    timeframe    = Column(String(6),  nullable=False)
    timestamp    = Column(DateTime,   nullable=False, index=True)
    open         = Column(Float, nullable=False)
    high         = Column(Float, nullable=False)
    low          = Column(Float, nullable=False)
    close        = Column(Float, nullable=False)
    volume       = Column(Float, nullable=False)
    quote_volume = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_ohlcv_sym_tf_ts", "symbol", "timeframe", "timestamp", unique=True),
    )

class BotLog(Base):
    __tablename__ = "bot_logs"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=_utcnow, index=True)
    level     = Column(String(10),  nullable=False)
    module    = Column(String(40),  nullable=True)
    message   = Column(Text,        nullable=False)

class BotState(Base):
    __tablename__ = "bot_state"

    key        = Column(String(64), primary_key=True)
    value_str  = Column(Text,    nullable=True)
    value_bool = Column(Boolean, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)

class APIMetric(Base):
    __tablename__ = "api_metrics"

    id                   = Column(Integer, primary_key=True, autoincrement=True)
    timestamp            = Column(DateTime, nullable=False, default=_utcnow, index=True)
    endpoint             = Column(String(100), nullable=False)
    latency_ms           = Column(Float,    nullable=True)
    rate_limit_remaining = Column(Integer,  nullable=True)
    success              = Column(Boolean,  nullable=False, default=True)
    error_msg            = Column(String(400), nullable=True)

class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    timestamp         = Column(DateTime, nullable=False, default=_utcnow, index=True)
    symbol            = Column(String(32), nullable=False, index=True)
    timeframe         = Column(String(6),  nullable=False)
    regime            = Column(String(32), nullable=False)
    regime_confidence = Column(Float,      nullable=False)
    adx_value         = Column(Float,      nullable=True)
    atr_pct           = Column(Float,      nullable=True)
    bb_width          = Column(Float,      nullable=True)
    ema_stack_score   = Column(Float,      nullable=True)

    __table_args__ = (
        Index("ix_regimes_symbol_ts",    "symbol", "timestamp"),
        Index("ix_regimes_symbol_tf_ts", "symbol", "timeframe", "timestamp"),
    )

class SignalScore(Base):
    __tablename__ = "signal_scores"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    timestamp        = Column(DateTime, nullable=False, default=_utcnow, index=True)
    symbol           = Column(String(32), nullable=False, index=True)
    strategy_profile = Column(String(64), nullable=False, index=True)

    total_score      = Column(Float, nullable=False)
    trend_score      = Column(Float, nullable=True)
    momentum_score   = Column(Float, nullable=True)
    strength_score   = Column(Float, nullable=True)
    volatility_score = Column(Float, nullable=True)
    pattern_score     = Column(Float, nullable=True)
    oscillator_score  = Column(Float, nullable=True)
    structure_score   = Column(Float, nullable=True)
    orderbook_score   = Column(Float, nullable=True)
    threshold_used   = Column(Float, nullable=True)
    regime           = Column(String(32), nullable=True)
    regime_confidence = Column(Float,     nullable=True)
    trigger_met      = Column(Boolean,    nullable=False, default=False)
    signal_type      = Column(String(16), nullable=True)
    action_taken     = Column(String(32), nullable=True)
    rejection_reason = Column(Text,       nullable=True)
    related_trade_id   = Column(Integer, nullable=True)
    current_price      = Column(Float,   nullable=True)
    suggested_sl       = Column(Float,   nullable=True)
    suggested_tp       = Column(Float,   nullable=True)
    nearest_support    = Column(Float,   nullable=True)
    nearest_resistance = Column(Float,   nullable=True)
    fib_support        = Column(Float,   nullable=True)
    fib_resistance     = Column(Float,   nullable=True)
    signal_confidence  = Column(Float,   nullable=True)

    __table_args__ = (
        Index("ix_scores_symbol_ts",      "symbol", "timestamp"),
        Index("ix_scores_symbol_profile", "symbol", "strategy_profile"),
        Index("ix_scores_profile_ts",     "strategy_profile", "timestamp"),
    )

class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    computed_at      = Column(DateTime, nullable=False, default=_utcnow, index=True)
    lookback_days    = Column(Integer,  nullable=False)
    scope            = Column(String(120), nullable=False, index=True)

    total_trades     = Column(Integer, nullable=False, default=0)
    win_rate         = Column(Float,   nullable=True)
    profit_factor    = Column(Float,   nullable=True)
    avg_score_wins   = Column(Float,   nullable=True)
    avg_score_losses = Column(Float,   nullable=True)
    best_regime      = Column(String(32), nullable=True)
    worst_regime     = Column(String(32), nullable=True)
    sufficient_data  = Column(Boolean,    nullable=False, default=False)
    summary_json     = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_perf_scope_lookback", "scope", "lookback_days", unique=True),
    )

class ParameterHistory(Base):
    __tablename__ = "parameter_history"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    timestamp        = Column(DateTime, nullable=False, default=_utcnow, index=True)
    symbol           = Column(String(32), nullable=False, index=True)
    profile          = Column(String(64), nullable=False, index=True)
    parameter_name   = Column(String(64), nullable=False)

    old_value        = Column(String(200), nullable=True)
    new_value        = Column(String(200), nullable=True)
    value_delta      = Column(String(200), nullable=True)  
    reason           = Column(Text,        nullable=True)
    approved_by      = Column(String(64),  nullable=True)
    performance_before = Column(Text, nullable=True)
    performance_after  = Column(Text, nullable=True)
    trades_after_apply = Column(Integer, nullable=True, default=0)
    outcome            = Column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_param_hist_symbol_profile", "symbol", "profile"),
        Index("ix_param_hist_param",          "symbol", "profile", "parameter_name"),
    )

class WatchlistOverride(Base):
    """
    Tabel hot-reload universe_watchlist tanpa restart bot.
    Titik 2 (CoinSwap), 3 (_try_shadow_trade), 4 (_cleanup_shadow_trade)
    cukup tulis/update baris di sini — bot baca tiap cycle otomatis.
    """
    __tablename__ = "universe_overrides"

    id         = Column(Integer,     primary_key=True, autoincrement=True)
    symbol     = Column(String(32),  nullable=False, unique=True, index=True)
    source     = Column(String(32),  nullable=False)
    is_active  = Column(Boolean,     default=True,  nullable=False, index=True)
    added_at   = Column(DateTime,    nullable=False, default=_utcnow)
    updated_at = Column(DateTime,    nullable=False, default=_utcnow, onupdate=_utcnow)
    notes      = Column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_universe_overrides_active", "is_active"),
    )


class DatabaseManager:

    MAX_SNAPSHOTS        = 10_000
    MAX_METRICS          = 1_000
    MAX_LOGS             = 5_000
    MAX_REGIMES          = 30_000
    MAX_SIGNAL_SCORES    = 50_000
    MAX_PARAMETER_HISTORY = 1_000

    SNAPSHOT_DEDUP_SECS = 55

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._pg = _is_postgres(database_url)

        if self._pg:
            self._engine = create_async_engine(
                database_url,
                echo=False,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                pool_timeout=30,
                pool_recycle=1800,
            )
            log.info("Database: PostgreSQL mode (pool_size=10, max_overflow=20)")
        else:
            self._engine = create_async_engine(
                database_url,
                echo=False,
                pool_pre_ping=True,
                connect_args={"check_same_thread": False},
            )

            @event.listens_for(self._engine.sync_engine, "connect")
            def _set_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA temp_store=MEMORY")
                cur.execute("PRAGMA mmap_size=268435456")
                cur.execute("PRAGMA cache_size=-32000")
                cur.close()

            log.info("Database: SQLite mode (WAL) — gunakan PostgreSQL untuk production!")

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def init_db(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Auto-migration: tambah kolom baru ke tabel existing
            if self._pg:
                await conn.execute(text(
                    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS "
                    "highest_price DOUBLE PRECISION"
                ))
            else:
                try:
                    await conn.execute(text(
                        "ALTER TABLE positions ADD COLUMN highest_price REAL"
                    ))
                except Exception:
                    pass  # Kolom sudah ada — aman diabaikan
                # Auto-migration signal_scores
                new_cols = [
                    "current_price REAL",
                    "suggested_sl REAL",
                    "suggested_tp REAL",
                    "nearest_support REAL",
                    "nearest_resistance REAL",
                    "fib_support REAL",
                    "fib_resistance REAL",
                    "signal_confidence REAL",
                ]
                for col in new_cols:
                    try:
                        await conn.execute(text(
                            "ALTER TABLE signal_scores ADD COLUMN " + col
                        ))
                    except Exception:
                        pass  # Kolom sudah ada
        log.info(
            "Database schema verified/created (%s). Tabel baru v7.0: "
            "market_regimes, signal_scores, performance_snapshots, parameter_history.",
            "PostgreSQL" if self._pg else "SQLite",
        )

    async def close(self) -> None:
        await self._engine.dispose()
        log.info("Database connection pool closed.")

    def _session(self) -> AsyncSession:
        return self._session_factory()

    async def get_bot_state(self, key: str) -> Optional[str]:
        try:
            async with self._session() as s:
                result = await s.execute(select(BotState).where(BotState.key == key))
                row = result.scalar_one_or_none()
                return row.value_str if row else None
        except Exception as e:
            log.error("get_bot_state(%s) error: %s", key, e)
            return None

    async def set_bot_state(self, key: str, value: str) -> None:
        try:
            async with self._session() as s:
                result = await s.execute(select(BotState).where(BotState.key == key))
                row = result.scalar_one_or_none()
                if row:
                    row.value_str  = value
                    row.updated_at = _utcnow()
                else:
                    s.add(BotState(key=key, value_str=value, updated_at=_utcnow()))
                await s.commit()
        except Exception as e:
            log.error("set_bot_state(%s) error: %s", key, e)

    async def clear_bot_state(self, key: str) -> None:
        try:
            async with self._session() as s:
                await s.execute(delete(BotState).where(BotState.key == key))
                await s.commit()
        except Exception as e:
            log.error("clear_bot_state(%s) error: %s", key, e)

    async def save_trade(self, trade_data: dict) -> Trade:
        async with self._session() as s:
            try:
                trade = Trade(**trade_data)
                s.add(trade)
                await s.commit()
                await s.refresh(trade)
                return trade
            except IntegrityError:
                await s.rollback()
                log.warning("save_trade: duplikat order_id=%s — skip insert, ambil existing.", trade_data.get("order_id"))
                result = await s.execute(
                    select(Trade).where(Trade.order_id == trade_data["order_id"])
                )
                return result.scalar_one()

    async def get_trade_by_order_id(self, order_id: str) -> Optional[Trade]:
        async with self._session() as s:
            result = await s.execute(
                select(Trade).where(Trade.order_id == order_id)
            )
            return result.scalar_one_or_none()

    async def get_recent_trades(
        self,
        limit:   int                        = 100,
        symbol:  Optional[str]              = None,
        profile: Optional[str]              = None,
        since:   Optional[datetime]         = None,
    ) -> List[Trade]:

        async with self._session() as s:
            q = select(Trade).order_by(desc(Trade.timestamp))
            if symbol:
                q = q.where(Trade.symbol == symbol)
            if profile:
                q = q.where(Trade.strategy_profile == profile)
            if since:
                q = q.where(Trade.timestamp >= since)
            q = q.limit(min(limit, 500))
            result = await s.execute(q)
            return list(result.scalars().all())

    async def get_trade_stats(
        self,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
        limit:   int           = 50,
    ) -> Optional[Dict[str, Any]]:

        async with self._session() as s:
            q = (
                select(Trade)
                .where(Trade.realized_pnl_pct.isnot(None))
                .order_by(desc(Trade.timestamp))
            )
            if symbol:
                q = q.where(Trade.symbol == symbol)
            if profile:
                q = q.where(Trade.strategy_profile == profile)
            q = q.limit(min(limit, 500))
            result = await s.execute(q)
            trades = list(result.scalars().all())

        if not trades:
            return None

        wins   = [t for t in trades if (t.realized_pnl_pct or 0) > 0]
        losses = [t for t in trades if (t.realized_pnl_pct or 0) <= 0]

        win_count   = len(wins)
        loss_count  = len(losses)
        total       = win_count + loss_count

        avg_win_pct  = sum(t.realized_pnl_pct for t in wins)  / win_count  if win_count  else 0.0
        avg_loss_pct = sum(abs(t.realized_pnl_pct) for t in losses) / loss_count if loss_count else 0.0

        gross_profit = sum(t.realized_pnl_pct for t in wins)
        gross_loss   = sum(t.realized_pnl_pct for t in losses)  

        profit_factor = (
            abs(gross_profit / gross_loss)
            if gross_loss != 0 else float("inf")
        )

        return {
            "total_trades":  total,
            "win_count":     win_count,
            "loss_count":    loss_count,
            "win_rate":      win_count / total if total > 0 else 0.0,
            "avg_win_pct":   round(avg_win_pct,  4),
            "avg_loss_pct":  round(avg_loss_pct, 4),
            "profit_factor": round(profit_factor, 4),
            "gross_profit":  round(gross_profit, 4),
            "gross_loss":    round(gross_loss,   4),
        }

    async def update_trade_pnl(
        self,
        order_id:         str,
        realized_pnl:     float,
        realized_pnl_pct: float,
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(Trade)
                .where(Trade.order_id == order_id)
                .values(
                    realized_pnl=round(realized_pnl, 8),
                    realized_pnl_pct=round(realized_pnl_pct, 6),
                )
            )
            await s.commit()

    async def append_trade_note(self, trade_id: int, note: str) -> None:
        try:
            async with self._session() as s:
                result = await s.execute(select(Trade).where(Trade.id == trade_id))
                t = result.scalar_one_or_none()
                if t:
                    t.notes = f"{t.notes} | {note}" if t.notes else note
                    await s.commit()
        except Exception as e:
            log.debug("append_trade_note non-critical error: %s", e)

    async def get_trades_with_regime(
        self,
        lookback_days: int            = 30,
        symbol:        Optional[str]  = None,
        profile:       Optional[str]  = None,
    ) -> List[Dict[str, Any]]:

        from datetime import timedelta
        since = _utcnow() - timedelta(days=lookback_days)

        async with self._session() as s:
            q = (
                select(Trade)
                .where(
                    Trade.timestamp >= since,
                    Trade.realized_pnl_pct.isnot(None),
                )
                .order_by(desc(Trade.timestamp))
            )
            if symbol:
                q = q.where(Trade.symbol == symbol)
            if profile:
                q = q.where(Trade.strategy_profile == profile)

            result = await s.execute(q)
            trades = list(result.scalars().all())

        rows = []
        for t in trades:
            rows.append({
                "id":               t.id,
                "timestamp":        t.timestamp,
                "symbol":           t.symbol,
                "strategy_profile": t.strategy_profile or t.strategy_name,
                "realized_pnl":     t.realized_pnl or 0.0,
                "realized_pnl_pct": t.realized_pnl_pct or 0.0,
                "is_win":           (t.realized_pnl_pct or 0) > 0,
            })

        if not rows:
            return rows

        symbols_in_result = list({r["symbol"] for r in rows})
        async with self._session() as s:
            # [BUG-FIX] Filter action_taken terlalu sempit — gagal korelasi regime/score
            # untuk trade nyata.
            # Sebelumnya: hanya cocok dengan literal "EXECUTE_CANDIDATE" (string yang
            # ditulis intelligence/scorer.py). Tapi alur eksekusi nyata lewat
            # intelligence/commander.py menyimpan action_taken sebagai "execute"/"EXECUTE"
            # (dari DecisionAction enum, lihat core/models.py), bukan "EXECUTE_CANDIDATE".
            # Akibatnya korelasi regime/score di sini selalu meleset ke default
            # "undefined"/50.0 untuk hampir semua trade produksi — datanya dikonsumsi
            # learning/analytics.py (multi-callsite) untuk evaluasi performa per-regime,
            # jadi data pembelajaran jadi bias.
            # Sekarang: cocokkan case-insensitive ke varian "EXECUTE_CANDIDATE"/"EXECUTE"
            # yang benar-benar dipakai di codebase (lihat scorer.py & commander.py).
            score_q = (
                select(SignalScore)
                .where(
                    SignalScore.symbol.in_(symbols_in_result),
                    SignalScore.timestamp >= since,
                    func.upper(SignalScore.action_taken).in_(
                        ["EXECUTE_CANDIDATE", "EXECUTE"]
                    ),
                )
                .order_by(SignalScore.symbol, desc(SignalScore.timestamp))
            )
            score_result = await s.execute(score_q)
            scores = list(score_result.scalars().all())

        score_lookup: Dict[str, List[Tuple]] = {}
        for sc in scores:
            score_lookup.setdefault(sc.symbol, []).append(
                (sc.timestamp, sc.regime or "undefined", sc.total_score or 50.0)
            )

        for row in rows:
            sym = row["symbol"]
            trade_ts = row["timestamp"]
            candidates = score_lookup.get(sym, [])
            best_regime = "undefined"
            best_score  = 50.0
            best_delta  = None

            for sc_ts, sc_regime, sc_score in candidates:
                delta = abs((trade_ts - sc_ts).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta  = delta
                    best_regime = sc_regime
                    best_score  = sc_score

            row["regime"]      = best_regime
            row["entry_score"] = best_score

        return rows

    async def get_score_vs_outcome(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
        profile:       Optional[str] = None,
        min_score:     float         = 0.0,
    ) -> List[Dict[str, Any]]:
        from datetime import timedelta
        since = _utcnow() - timedelta(days=lookback_days)

        async with self._session() as s:
            q = (
                select(SignalScore)
                .where(
                    SignalScore.timestamp >= since,
                    SignalScore.related_trade_id.isnot(None),
                    SignalScore.total_score >= min_score,
                )
                .order_by(desc(SignalScore.timestamp))
            )
            if symbol:
                q = q.where(SignalScore.symbol == symbol)
            if profile:
                q = q.where(SignalScore.strategy_profile == profile)

            result = await s.execute(q)
            scores = list(result.scalars().all())

        if not scores:
            return []

        trade_ids = [sc.related_trade_id for sc in scores]
        async with self._session() as s:
            trade_result = await s.execute(
                select(Trade).where(Trade.id.in_(trade_ids))
            )
            trades_map = {t.id: t for t in trade_result.scalars().all()}

        rows = []
        for sc in scores:
            trade = trades_map.get(sc.related_trade_id)
            if trade is None:
                continue
            rows.append({
                "signal_score_id":  sc.id,
                "symbol":           sc.symbol,
                "strategy_profile": sc.strategy_profile,
                "timestamp":        sc.timestamp,
                "total_score":      sc.total_score,
                "trend_score":      sc.trend_score,
                "momentum_score":   sc.momentum_score,
                "strength_score":   sc.strength_score,
                "volatility_score": sc.volatility_score,
                "pattern_score":    sc.pattern_score,
                "oscillator_score": sc.oscillator_score,
                "structure_score":  sc.structure_score,
                "orderbook_score":  sc.orderbook_score,
                "regime":           sc.regime or "undefined",
                "pnl_pct":          trade.realized_pnl_pct or 0.0,
                "is_win":           (trade.realized_pnl_pct or 0) > 0,
            })

        return rows

    async def upsert_position(self, symbol: str, data: dict) -> Position:
        async with self._session() as s:
            result = await s.execute(
                select(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .with_for_update()
            )
            pos = result.scalar_one_or_none()
            if pos:
                for k, v in data.items():
                    setattr(pos, k, v)
            else:
                pos = Position(symbol=symbol, **data)
                s.add(pos)
            await s.commit()
            await s.refresh(pos)
            return pos

    async def update_position_sl(self, symbol: str, new_sl: float) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .values(stop_loss_price=round(new_sl, 8))
            )
            await s.commit()


    async def update_position_highest_price(self, symbol: str, price: float) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .values(highest_price=round(price, 8))
            )
            await s.commit()
    async def update_position_price(
        self,
        symbol:             str,
        price:              float,
        unrealized_pnl:     float,
        unrealized_pnl_pct: float,
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .values(
                    current_price=round(price, 8),
                    unrealized_pnl=round(unrealized_pnl, 8),
                    unrealized_pnl_pct=round(unrealized_pnl_pct, 6),
                )
            )
            await s.commit()

    async def get_open_positions(self) -> List[Position]:
        async with self._session() as s:
            result = await s.execute(
                select(Position).where(Position.is_open == True)
            )
            return list(result.scalars().all())

    async def get_open_position_by_symbol(self, symbol: str) -> Optional[Position]:
        async with self._session() as s:
            result = await s.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.is_open == True,
                )
            )
            return result.scalar_one_or_none()

    async def mark_position_closing(self, symbol: str) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .values(is_closing=True)
            )
            await s.commit()

    async def update_position_entry_fee(self, symbol: str, fee: float) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.symbol == symbol, Position.is_open == True)
                .values(entry_fee_actual=round(fee, 8))
            )
            await s.commit()

    async def get_closing_positions(self) -> List[Position]:
        async with self._session() as s:
            result = await s.execute(
                select(Position).where(
                    Position.is_open == True,
                    Position.is_closing == True,
                )
            )
            return list(result.scalars().all())

    async def close_position(
        self,
        symbol:       str,
        exit_price:   float,
        realized_pnl: float,
    ) -> Optional[Position]:
        async with self._session() as s:
            result = await s.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.is_open == True,
                )
            )
            pos = result.scalar_one_or_none()
            if not pos:
                log.warning("close_position: no open position for %s", symbol)
                return None

            cost = (pos.entry_price or 0) * (pos.amount or 0)
            realized_pnl_pct = (realized_pnl / cost * 100) if cost > 0 else 0.0

            pos.is_open            = False
            pos.is_closing         = False
            pos.exit_time          = _utcnow()
            pos.current_price      = round(exit_price, 8)
            pos.realized_pnl       = round(realized_pnl, 8)
            pos.realized_pnl_pct   = round(realized_pnl_pct, 6)
            pos.unrealized_pnl     = 0.0
            pos.unrealized_pnl_pct = 0.0

            if pos.entry_order_id:
                await s.execute(
                    update(Trade)
                    .where(Trade.order_id == pos.entry_order_id)
                    .values(
                        realized_pnl=round(realized_pnl, 8),
                        realized_pnl_pct=round(realized_pnl_pct, 6),
                    )
                )
            await s.commit()
        return pos

    async def save_snapshot(self, data: dict) -> None:
        async with self._session() as s:
            last_result = await s.execute(
                select(PortfolioSnapshot)
                .order_by(desc(PortfolioSnapshot.timestamp))
                .limit(1)
            )
            last_snap = last_result.scalar_one_or_none()
            if last_snap is not None:
                age_secs = (_utcnow() - last_snap.timestamp).total_seconds()
                if age_secs < self.SNAPSHOT_DEDUP_SECS:
                    log.debug(
                        "Snapshot skipped: last was %.1fs ago (< %ds dedup)",
                        age_secs, self.SNAPSHOT_DEDUP_SECS,
                    )
                    return
            s.add(PortfolioSnapshot(**data))
            await s.commit()
        await self._prune_snapshots()

    async def get_equity_curve(self, limit: int = 500) -> List[PortfolioSnapshot]:
        async with self._session() as s:
            result = await s.execute(
                select(PortfolioSnapshot)
                .order_by(desc(PortfolioSnapshot.timestamp))
                .limit(limit)
            )
            return list(reversed(result.scalars().all()))

    async def _prune_snapshots(self) -> None:
        async with self._session() as s:
            sub = (
                select(PortfolioSnapshot.timestamp)
                .order_by(desc(PortfolioSnapshot.timestamp))
                .offset(self.MAX_SNAPSHOTS)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(
                delete(PortfolioSnapshot).where(PortfolioSnapshot.timestamp <= sub)
            )
            await s.commit()

    async def save_log(self, level: str, module: str, message: str) -> None:
        try:
            async with self._session() as s:
                s.add(BotLog(level=level, module=module, message=message[:2000]))
                await s.commit()
            await self._prune_logs()
        except Exception as e:
            log.error("save_log failed: %s", e)

    async def get_recent_logs(self, limit: int = 100) -> List[BotLog]:
        async with self._session() as s:
            result = await s.execute(
                select(BotLog)
                .order_by(desc(BotLog.timestamp))
                .limit(min(limit, 500))
            )
            return list(result.scalars().all())

    async def _prune_logs(self) -> None:
        async with self._session() as s:
            sub = (
                select(BotLog.timestamp)
                .order_by(desc(BotLog.timestamp))
                .offset(self.MAX_LOGS)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(delete(BotLog).where(BotLog.timestamp <= sub))
            await s.commit()

    async def save_api_metric(
        self,
        endpoint:             str,
        latency_ms:           float,
        success:              bool          = True,
        rate_limit_remaining: Optional[int] = None,
        error_msg:            Optional[str] = None,
    ) -> None:
        try:
            async with self._session() as s:
                s.add(APIMetric(
                    endpoint=endpoint,
                    latency_ms=round(latency_ms, 2),
                    success=success,
                    rate_limit_remaining=rate_limit_remaining,
                    error_msg=(error_msg[:400] if error_msg else None),
                ))
                await s.commit()
            await self._prune_metrics()
        except Exception as e:
            log.debug("save_api_metric non-critical error: %s", e)

    async def get_api_metrics(self, limit: int = 50) -> List[APIMetric]:
        async with self._session() as s:
            result = await s.execute(
                select(APIMetric)
                .order_by(desc(APIMetric.timestamp))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_avg_latency_ms(self, endpoint: Optional[str] = None) -> float:
        async with self._session() as s:
            q = select(APIMetric).where(APIMetric.success == True)
            if endpoint:
                q = q.where(APIMetric.endpoint == endpoint)
            result = await s.execute(
                q.order_by(desc(APIMetric.timestamp)).limit(50)
            )
            metrics = result.scalars().all()
            vals = [m.latency_ms for m in metrics if m.latency_ms is not None]
            return sum(vals) / len(vals) if vals else 0.0

    async def _prune_metrics(self) -> None:
        async with self._session() as s:
            sub = (
                select(APIMetric.timestamp)
                .order_by(desc(APIMetric.timestamp))
                .offset(self.MAX_METRICS)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(
                delete(APIMetric).where(APIMetric.timestamp <= sub)
            )
            await s.commit()

    async def get_today_trade_count(self) -> int:
        now_utc     = _utcnow()
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._session() as s:
            result = await s.execute(
                select(func.count(Trade.id)).where(Trade.timestamp >= today_start)
            )
            return result.scalar_one() or 0

    async def save_market_regime(
        self,
        symbol:            str,
        timeframe:         str,
        regime:            str,
        regime_confidence: float,
        adx_value:         Optional[float] = None,
        atr_pct:           Optional[float] = None,
        bb_width:          Optional[float] = None,
        ema_stack_score:   Optional[float] = None,
    ) -> None:
        try:
            async with self._session() as s:
                s.add(MarketRegimeRecord(
                    symbol=symbol,
                    timeframe=timeframe,
                    regime=regime,
                    regime_confidence=round(regime_confidence, 4),
                    adx_value=round(adx_value, 2) if adx_value is not None else None,
                    atr_pct=round(atr_pct, 4) if atr_pct is not None else None,
                    bb_width=round(bb_width, 6) if bb_width is not None else None,
                    ema_stack_score=round(ema_stack_score, 2) if ema_stack_score is not None else None,
                ))
                await s.commit()
            await self._prune_regimes()
        except Exception as e:
            log.debug("save_market_regime non-critical error: %s", e)

    async def get_regime_history(
        self,
        symbol:    str,
        timeframe: Optional[str] = None,
        limit:     int           = 100,
    ) -> List[Dict[str, Any]]:

        async with self._session() as s:
            q = (
                select(MarketRegimeRecord)
                .where(MarketRegimeRecord.symbol == symbol)
                .order_by(desc(MarketRegimeRecord.timestamp))
            )
            if timeframe:
                q = q.where(MarketRegimeRecord.timeframe == timeframe)
            q = q.limit(min(limit, 1000))
            result = await s.execute(q)
            records = result.scalars().all()

        return [
            {
                "timestamp":         r.timestamp,
                "symbol":            r.symbol,
                "timeframe":         r.timeframe,
                "regime":            r.regime,
                "regime_confidence": r.regime_confidence,
                "adx_value":         r.adx_value,
                "atr_pct":           r.atr_pct,
                "bb_width":          r.bb_width,
                "ema_stack_score":   r.ema_stack_score,
            }
            for r in records
        ]

    async def get_regime_distribution(
        self,
        lookback_days: int           = 30,
        symbol:        Optional[str] = None,
    ) -> Dict[str, int]:
        from datetime import timedelta
        since = _utcnow() - timedelta(days=lookback_days)

        async with self._session() as s:
            q = (
                select(MarketRegimeRecord.regime, func.count(MarketRegimeRecord.id))
                .where(MarketRegimeRecord.timestamp >= since)
                .group_by(MarketRegimeRecord.regime)
            )
            if symbol:
                q = q.where(MarketRegimeRecord.symbol == symbol)
            result = await s.execute(q)
            return {row[0]: row[1] for row in result.all()}

    async def _prune_regimes(self) -> None:
        async with self._session() as s:
            sub = (
                select(MarketRegimeRecord.timestamp)
                .order_by(desc(MarketRegimeRecord.timestamp))
                .offset(self.MAX_REGIMES)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(
                delete(MarketRegimeRecord)
                .where(MarketRegimeRecord.timestamp <= sub)
            )
            await s.commit()

    async def save_signal_score(
        self,
        symbol:           str,
        strategy_profile: str,
        total_score:      float,
        trend_score:      float           = 50.0,
        momentum_score:   float           = 50.0,
        strength_score:   float           = 50.0,
        volatility_score: float           = 50.0,
        pattern_score:    float           = 50.0,
        oscillator_score: float           = 50.0,
        structure_score:  float           = 50.0,
        orderbook_score:  float           = 50.0,
        threshold_used:   Optional[float] = None,
        regime:           Optional[str]   = None,
        regime_confidence: Optional[float] = None,
        trigger_met:      bool            = False,
        signal_type:      Optional[str]   = None,
        action_taken:     Optional[str]   = None,
        rejection_reason: Optional[str]   = None,
        related_trade_id:   Optional[int]   = None,
        current_price:      Optional[float] = None,
        suggested_sl:       Optional[float] = None,
        suggested_tp:       Optional[float] = None,
        nearest_support:    Optional[float] = None,
        nearest_resistance: Optional[float] = None,
        fib_support:        Optional[float] = None,
        fib_resistance:     Optional[float] = None,
        signal_confidence:  Optional[float] = None,
    ) -> Optional[int]:
        try:
            async with self._session() as s:
                rec = SignalScore(
                    symbol=symbol,
                    strategy_profile=strategy_profile,
                    total_score=round(total_score, 2),
                    trend_score=round(trend_score, 2) if trend_score is not None else None,
                    momentum_score=round(momentum_score, 2) if momentum_score is not None else None,
                    strength_score=round(strength_score, 2) if strength_score is not None else None,
                    volatility_score=round(volatility_score, 2) if volatility_score is not None else None,
                    pattern_score=round(pattern_score, 2) if pattern_score is not None else None,
                    oscillator_score=round(oscillator_score, 2) if oscillator_score is not None else None,
                    structure_score=round(structure_score, 2) if structure_score is not None else None,
                    orderbook_score=round(orderbook_score, 2) if orderbook_score is not None else None,
                    threshold_used=round(threshold_used, 2) if threshold_used is not None else None,
                    regime=regime,
                    regime_confidence=round(regime_confidence, 4) if regime_confidence is not None else None,
                    trigger_met=trigger_met,
                    signal_type=signal_type,
                    action_taken=action_taken,
                    rejection_reason=(rejection_reason[:1000] if rejection_reason else None),
                    related_trade_id=related_trade_id,
                    current_price=round(current_price, 8) if current_price else None,
                    suggested_sl=round(suggested_sl, 8) if suggested_sl else None,
                    suggested_tp=round(suggested_tp, 8) if suggested_tp else None,
                    nearest_support=round(nearest_support, 8) if nearest_support else None,
                    nearest_resistance=round(nearest_resistance, 8) if nearest_resistance else None,
                    fib_support=round(fib_support, 8) if fib_support else None,
                    fib_resistance=round(fib_resistance, 8) if fib_resistance else None,
                    signal_confidence=round(signal_confidence, 4) if signal_confidence else None,
                )
                s.add(rec)
                await s.commit()
                await s.refresh(rec)
                row_id = rec.id
            await self._prune_signal_scores()
            return row_id
        except Exception as e:
            log.debug("save_signal_score non-critical error: %s", e)
            return None

    async def link_signal_to_trade(
        self,
        signal_score_id: int,
        trade_id:        int,
    ) -> None:
        try:
            async with self._session() as s:
                await s.execute(
                    update(SignalScore)
                    .where(SignalScore.id == signal_score_id)
                    .values(related_trade_id=trade_id)
                )
                await s.commit()
        except Exception as e:
            log.debug("link_signal_to_trade non-critical error: %s", e)

    async def get_signal_scores(
        self,
        symbol:           Optional[str] = None,
        strategy_profile: Optional[str] = None,
        limit:            int           = 100,
        only_executed:    bool          = False,
    ) -> List[Dict[str, Any]]:

        async with self._session() as s:
            q = (
                select(SignalScore)
                .order_by(desc(SignalScore.timestamp))
            )
            if symbol:
                q = q.where(SignalScore.symbol == symbol)
            if strategy_profile:
                q = q.where(SignalScore.strategy_profile == strategy_profile)
            if only_executed:
                q = q.where(SignalScore.related_trade_id.isnot(None))
            q = q.limit(min(limit, 1000))
            result = await s.execute(q)
            records = result.scalars().all()

        return [
            {
                "id":                rec.id,
                "timestamp":         rec.timestamp,
                "symbol":            rec.symbol,
                "strategy_profile":  rec.strategy_profile,
                "total_score":       rec.total_score,
                "trend_score":       rec.trend_score,
                "momentum_score":    rec.momentum_score,
                "strength_score":    rec.strength_score,
                "volatility_score":  rec.volatility_score,
                "pattern_score":     rec.pattern_score,
                "threshold_used":    rec.threshold_used,
                "regime":            rec.regime,
                "regime_confidence": rec.regime_confidence,
                "trigger_met":       rec.trigger_met,
                "signal_type":       rec.signal_type,
                "action_taken":      rec.action_taken,
                "rejection_reason":  rec.rejection_reason,
                "related_trade_id":  rec.related_trade_id,
            }
            for rec in records
        ]

    async def get_latest_signal_score(self, symbol: str) -> Optional[SignalScore]:
        async with self._session() as s:
            result = await s.execute(
                select(SignalScore)
                .where(SignalScore.symbol == symbol)
                .order_by(desc(SignalScore.timestamp))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_latest_regime(self, symbol: str) -> Optional[MarketRegimeRecord]:
        async with self._session() as s:
            result = await s.execute(
                select(MarketRegimeRecord)
                .where(MarketRegimeRecord.symbol == symbol)
                .order_by(desc(MarketRegimeRecord.timestamp))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_indicator_effectiveness(self, lookback_days: int = 30) -> Dict[str, Any]:
        # Minimal safe implementation so API doesn't crash; detailed report is provided
        # via learning/analytics.py (AnalyticsEngine).
        try:
            rows = await self.get_score_vs_outcome(lookback_days=lookback_days)
            if not rows:
                return {}
            # Return only basic counts; keep response stable and lightweight.
            return {
                "sample_size": len(rows),
                "note": "Gunakan endpoint /api/analytics/indicator_effectiveness untuk report lengkap.",
            }
        except Exception as e:
            # [BUG-FIX] except Exception: pass tanpa log — error asli (misal bug di
            # get_score_vs_outcome) tertutup oleh dict kosong, terlihat sama dengan
            # "memang belum ada data". Sekarang: di-log agar bisa dibedakan.
            log.debug("get_indicator_effectiveness error: %s", e)
            return {}

    async def get_pending_suggestions(self, limit: int = 100) -> List[Dict[str, Any]]:
        # Suggestions are stored in parameter_history with outcome 'pending'
        async with self._session() as s:
            result = await s.execute(
                select(ParameterHistory)
                .where(ParameterHistory.outcome == "pending")
                .order_by(desc(ParameterHistory.timestamp))
                .limit(min(limit, 500))
            )
            rows = list(result.scalars().all())

        suggestions: List[Dict[str, Any]] = []
        for r in rows:
            confidence = None
            projected = None
            if r.performance_before:
                try:
                    pb = json.loads(r.performance_before)
                    confidence = pb.get("confidence")
                    projected = (
                        pb.get("projected_improvement")
                        or pb.get("projected_improvement_pct")
                    )
                except Exception:
                    pass

            suggestions.append({
                "id": r.id,
                "timestamp": r.timestamp,
                "symbol": r.symbol,
                "profile": r.profile,
                "parameter_name": r.parameter_name,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "reason": (r.reason or ""),
                "confidence": confidence,
                "projected_improvement": projected,
                "status": r.outcome,
            })

        return suggestions

    async def get_latest_score_per_symbol(self) -> Dict[str, Dict[str, Any]]:

        async with self._session() as s:
            sub = (
                select(
                    SignalScore.symbol,
                    func.max(SignalScore.timestamp).label("max_ts"),
                )
                .group_by(SignalScore.symbol)
                .subquery()
            )
            q = (
                select(SignalScore)
                .join(
                    sub,
                    (SignalScore.symbol == sub.c.symbol)
                    & (SignalScore.timestamp == sub.c.max_ts),
                )
            )
            result = await s.execute(q)
            records = result.scalars().all()

        return {
            rec.symbol: {
                "total_score":      rec.total_score,
                "trend_score":      rec.trend_score,
                "momentum_score":   rec.momentum_score,
                "strength_score":   rec.strength_score,
                "volatility_score": rec.volatility_score,
                "pattern_score":    rec.pattern_score,
                "regime":           rec.regime,
                "trigger_met":      rec.trigger_met,
                "signal_type":      rec.signal_type,
                "timestamp":        rec.timestamp,
                "threshold_used":   rec.threshold_used,
            }
            for rec in records
        }

    async def _prune_signal_scores(self) -> None:
        async with self._session() as s:
            sub = (
                select(SignalScore.timestamp)
                .order_by(desc(SignalScore.timestamp))
                .offset(self.MAX_SIGNAL_SCORES)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(
                delete(SignalScore).where(SignalScore.timestamp <= sub)
            )
            await s.commit()

    async def save_performance_snapshot(
        self,
        scope:           str,
        lookback_days:   int,
        total_trades:    int,
        win_rate:        Optional[float],
        profit_factor:   Optional[float],
        avg_score_wins:  Optional[float],
        avg_score_losses: Optional[float],
        best_regime:     Optional[str],
        worst_regime:    Optional[str],
        sufficient_data: bool,
        summary_json:    Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            json_str = json.dumps(summary_json, default=str) if summary_json else None

            async with self._session() as s:
                result = await s.execute(
                    select(PerformanceSnapshot).where(
                        PerformanceSnapshot.scope == scope,
                        PerformanceSnapshot.lookback_days == lookback_days,
                    )
                )
                existing = result.scalar_one_or_none()

                if existing:
                    existing.computed_at      = _utcnow()
                    existing.total_trades     = total_trades
                    existing.win_rate         = round(win_rate, 4) if win_rate is not None else None
                    existing.profit_factor    = round(profit_factor, 4) if profit_factor is not None else None
                    existing.avg_score_wins   = round(avg_score_wins, 2) if avg_score_wins is not None else None
                    existing.avg_score_losses = round(avg_score_losses, 2) if avg_score_losses is not None else None
                    existing.best_regime      = best_regime
                    existing.worst_regime     = worst_regime
                    existing.sufficient_data  = sufficient_data
                    existing.summary_json     = json_str
                else:
                    s.add(PerformanceSnapshot(
                        scope=scope,
                        lookback_days=lookback_days,
                        total_trades=total_trades,
                        win_rate=round(win_rate, 4) if win_rate is not None else None,
                        profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
                        avg_score_wins=round(avg_score_wins, 2) if avg_score_wins is not None else None,
                        avg_score_losses=round(avg_score_losses, 2) if avg_score_losses is not None else None,
                        best_regime=best_regime,
                        worst_regime=worst_regime,
                        sufficient_data=sufficient_data,
                        summary_json=json_str,
                    ))
                await s.commit()
        except Exception as e:
            log.error("save_performance_snapshot error: %s", e)

    async def get_latest_snapshot(
        self,
        scope:         str,
        lookback_days: int,
    ) -> Optional[Dict[str, Any]]:

        async with self._session() as s:
            result = await s.execute(
                select(PerformanceSnapshot).where(
                    PerformanceSnapshot.scope == scope,
                    PerformanceSnapshot.lookback_days == lookback_days,
                )
            )
            snap = result.scalar_one_or_none()

        if snap is None:
            return None

        age_hours = (_utcnow() - snap.computed_at).total_seconds() / 3600
        if age_hours > 2.0:
            return None

        summary = None
        if snap.summary_json:
            try:
                summary = json.loads(snap.summary_json)
            except json.JSONDecodeError:
                pass

        return {
            "computed_at":      snap.computed_at,
            "scope":            snap.scope,
            "lookback_days":    snap.lookback_days,
            "total_trades":     snap.total_trades,
            "win_rate":         snap.win_rate,
            "profit_factor":    snap.profit_factor,
            "avg_score_wins":   snap.avg_score_wins,
            "avg_score_losses": snap.avg_score_losses,
            "best_regime":      snap.best_regime,
            "worst_regime":     snap.worst_regime,
            "sufficient_data":  snap.sufficient_data,
            "summary":          summary,
        }

    async def get_all_snapshots(self) -> List[Dict[str, Any]]:

        async with self._session() as s:
            result = await s.execute(
                select(PerformanceSnapshot)
                .order_by(desc(PerformanceSnapshot.computed_at))
            )
            snaps = result.scalars().all()

        return [
            {
                "computed_at":      s.computed_at,
                "scope":            s.scope,
                "lookback_days":    s.lookback_days,
                "total_trades":     s.total_trades,
                "win_rate":         s.win_rate,
                "profit_factor":    s.profit_factor,
                "sufficient_data":  s.sufficient_data,
            }
            for s in snaps
        ]

    async def save_parameter_change(
        self,
        symbol:             str,
        profile:            str,
        parameter_name:     str,
        old_value:          Any,
        new_value:          Any,
        reason:             str            = "",
        approved_by:        str            = "manual",
        performance_before: Optional[Dict] = None,
    ) -> Optional[int]:
        try:
            delta = None
            try:
                delta = str(round(float(new_value) - float(old_value), 6))
            except (TypeError, ValueError):
                delta = f"{old_value} → {new_value}"

            perf_before_json = (
                json.dumps(performance_before, default=str)
                if performance_before else None
            )

            async with self._session() as s:
                rec = ParameterHistory(
                    symbol=symbol,
                    profile=profile,
                    parameter_name=parameter_name,
                    old_value=str(old_value)[:200] if old_value is not None else None,
                    new_value=str(new_value)[:200] if new_value is not None else None,
                    value_delta=str(delta)[:200] if delta is not None else None,
                    reason=reason,
                    approved_by=approved_by,
                    performance_before=perf_before_json,
                    outcome="pending",
                )
                s.add(rec)
                await s.commit()
                await s.refresh(rec)
                row_id = rec.id
            await self._prune_parameter_history()
            return row_id
        except Exception as e:
            log.error("save_parameter_change error: %s", e)
            return None

    async def update_parameter_outcome(
        self,
        record_id:          int,
        performance_after:  Dict[str, Any],
        trades_after_apply: int,
        outcome:            str,
    ) -> None:
        try:
            async with self._session() as s:
                await s.execute(
                    update(ParameterHistory)
                    .where(ParameterHistory.id == record_id)
                    .values(
                        performance_after=json.dumps(performance_after, default=str),
                        trades_after_apply=trades_after_apply,
                        outcome=outcome,
                    )
                )
                await s.commit()
        except Exception as e:
            log.error("update_parameter_outcome error: %s", e)

    async def get_parameter_history(
        self,
        symbol:  Optional[str] = None,
        profile: Optional[str] = None,
        limit:   int           = 100,
    ) -> List[Dict[str, Any]]:

        async with self._session() as s:
            q = (
                select(ParameterHistory)
                .order_by(desc(ParameterHistory.timestamp))
            )
            if symbol:
                q = q.where(ParameterHistory.symbol == symbol)
            if profile:
                q = q.where(ParameterHistory.profile == profile)
            q = q.limit(min(limit, 500))
            result = await s.execute(q)
            records = result.scalars().all()

        return [
            {
                "id":               r.id,
                "timestamp":        r.timestamp,
                "symbol":           r.symbol,
                "profile":          r.profile,
                "parameter_name":   r.parameter_name,
                "old_value":        r.old_value,
                "new_value":        r.new_value,
                "value_delta":      r.value_delta,
                "reason":           r.reason,
                "approved_by":      r.approved_by,
                "outcome":          r.outcome,
                "trades_after_apply": r.trades_after_apply,
            }
            for r in records
        ]

    async def get_pending_outcomes(
        self,
        min_trades_threshold: int = 30,
    ) -> List[Dict[str, Any]]:
        # [BUG-FIX] Parameter `min_trades_threshold` diterima tapi tidak pernah
        # dipakai untuk memfilter — tampak seolah-olah fungsi ini hanya
        # mengembalikan record yang sudah punya >= N trade sejak diterapkan.
        # Sebelumnya: tidak ada filter berbasis threshold sama sekali di query.
        # Sekarang: TIDAK diubah jadi filter `trades_after_apply >= min_trades_threshold`
        # karena kolom `trades_after_apply` baru terisi setelah evaluasi selesai
        # (lihat update_parameter_outcome) — defaultnya 0 selagi PENDING, jadi
        # memfilter di sini justru akan membuat fungsi ini selalu kosong.
        # Pemanggil (learning/meta_learner.py: check_pending_outcomes →
        # _evaluate_outcome) sudah melakukan pengecekan jumlah trade sendiri
        # secara terpisah (query ulang trade sejak parameter diubah). Parameter
        # ini sengaja dibiarkan tidak terpakai di sini untuk sekarang — cek ulang
        # saat audit learning/meta_learner.py (Tier 2) apakah dua mekanisme ini
        # harus disatukan.
        async with self._session() as s:
            result = await s.execute(
                select(ParameterHistory).where(
                    ParameterHistory.outcome == "pending",
                    ParameterHistory.performance_before.isnot(None),
                )
                .order_by(ParameterHistory.timestamp)
            )
            records = result.scalars().all()

        results = []
        for r in records:
            perf_before = None
            if r.performance_before:
                try:
                    perf_before = json.loads(r.performance_before)
                except json.JSONDecodeError:
                    pass

            results.append({
                "id":              r.id,
                "timestamp":       r.timestamp,
                "symbol":          r.symbol,
                "profile":         r.profile,
                "parameter_name":  r.parameter_name,
                "old_value":       r.old_value,
                "new_value":       r.new_value,
                "approved_by":     r.approved_by,
                "performance_before": perf_before,
                "trades_after_apply": r.trades_after_apply or 0,
            })

        return results

    async def _prune_parameter_history(self) -> None:
        async with self._session() as s:
            sub = (
                select(ParameterHistory.timestamp)
                .order_by(desc(ParameterHistory.timestamp))
                .offset(self.MAX_PARAMETER_HISTORY)
                .limit(1)
                .scalar_subquery()
            )
            await s.execute(
                delete(ParameterHistory)
                .where(ParameterHistory.timestamp <= sub)
            )
            await s.commit()

    async def get_db_health(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        tables = [
            ("trades",               Trade),
            ("positions_open",       None),
            ("portfolio_snapshots",  PortfolioSnapshot),
            ("bot_logs",             BotLog),
            ("api_metrics",          APIMetric),
            ("market_regimes",       MarketRegimeRecord),
            ("signal_scores",        SignalScore),
            ("performance_snapshots", PerformanceSnapshot),
            ("parameter_history",    ParameterHistory),
        ]

        async with self._session() as s:
            for name, model in tables:
                try:
                    if name == "positions_open":
                        result = await s.execute(
                            select(func.count(Position.id))
                            .where(Position.is_open == True)
                        )
                    else:
                        result = await s.execute(select(func.count(model.id)))
                    counts[name] = result.scalar_one() or 0
                except Exception as e:
                    # [BUG-FIX] except Exception: pass — kegagalan count tabel (misal
                    # tabel belum ter-migrasi) tertutup jadi -1 tanpa jejak log,
                    # padahal endpoint ini dipakai untuk health-check produksi.
                    # Sekarang: di-log agar operator tahu tabel mana yang gagal & kenapa.
                    counts[name] = -1
                    log.warning("get_db_health: gagal hitung tabel '%s': %s", name, e)

        return {
            "status":  "ok",
            "backend": "postgresql" if self._pg else "sqlite",
            "tables":  counts,
        }
    async def cleanup_old_data(
        self,
        portfolio_snapshots_days: int = 7,
        api_metrics_days: int = 3,
        bot_logs_days: int = 3,
        signal_scores_days: int = 30,
    ) -> Dict[str, int]:
        """Hapus data lama yang tidak dibutuhkan meta_learner & analytics."""
        from datetime import timedelta
        cutoffs = {
            "portfolio_snapshots": _utcnow() - timedelta(days=portfolio_snapshots_days),
            "api_metrics":         _utcnow() - timedelta(days=api_metrics_days),
            "bot_logs":            _utcnow() - timedelta(days=bot_logs_days),
            "signal_scores":       _utcnow() - timedelta(days=signal_scores_days),
        }
        models = {
            "portfolio_snapshots": PortfolioSnapshot,
            "api_metrics":         APIMetric,
            "bot_logs":            BotLog,
            "signal_scores":       SignalScore,
        }
        deleted = {}
        async with self._session() as s:
            async with s.begin():
                for name, model in models.items():
                    try:
                        result = await s.execute(
                            delete(model).where(model.timestamp < cutoffs[name])
                        )
                        deleted[name] = result.rowcount
                    except Exception as e:
                        deleted[name] = -1
                        log.warning("cleanup_old_data [%s] error: %s", name, e)
        # Vacuum SQLite untuk bebaskan disk
        if not self._pg:
            async with self._session() as s:
                await s.execute(text("VACUUM"))
        log.info("DB cleanup selesai: %s", deleted)
        return deleted

    # ── WatchlistOverride methods ──────────────────────────────────────────

    async def get_active_universe_overrides(self) -> List[str]:
        """Ambil semua symbol yang is_active=True dari universe_overrides."""
        async with self._session() as s:
            result = await s.execute(
                select(WatchlistOverride.symbol)
                .where(WatchlistOverride.is_active == True)
                .order_by(WatchlistOverride.added_at)
            )
            return [row[0] for row in result.fetchall()]

    async def upsert_universe_override(
        self,
        symbol: str,
        source: str,
        notes: str = "",
    ) -> None:
        """Tambah atau aktifkan kembali symbol di universe_overrides."""
        async with self._session() as s:
            async with s.begin():
                existing = await s.execute(
                    select(WatchlistOverride)
                    .where(WatchlistOverride.symbol == symbol)
                )
                row = existing.scalar_one_or_none()
                if row:
                    row.is_active  = True
                    row.source     = source
                    row.notes      = notes
                    row.updated_at = _utcnow()
                else:
                    s.add(WatchlistOverride(
                        symbol    = symbol,
                        source    = source,
                        is_active = True,
                        notes     = notes,
                        added_at  = _utcnow(),
                        updated_at= _utcnow(),
                    ))
        log.info("WatchlistOverride upsert: %s [%s]", symbol, source)

    async def deactivate_universe_override(self, symbol: str) -> None:
        """Nonaktifkan symbol dari universe_overrides (tanpa hapus baris)."""
        async with self._session() as s:
            async with s.begin():
                await s.execute(
                    update(WatchlistOverride)
                    .where(WatchlistOverride.symbol == symbol)
                    .values(is_active=False, updated_at=_utcnow())
                )
        log.info("WatchlistOverride deactivated: %s", symbol)
