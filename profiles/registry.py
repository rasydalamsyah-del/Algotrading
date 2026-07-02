"""
profiles/registry.py
AlgoTrader Pro v7.0 — "The Intelligence Pipeline"

"""

from __future__ import annotations

import logging
import asyncio
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from profiles.base_profile import (
    CoinProfile,
    ParameterBounds,
    PrimaryTriggerType,
    StrategyProfile,
    PROFILE_EMOJI,
    PROFILE_TIMEFRAME,
    TIMEFRAME_CONFIRMATION_MAP,
)
from profiles.thresholds import get_profile_thresholds, get_all_profile_names

log = logging.getLogger("profiles.registry")


def _fire_and_forget_db(coro_or_result) -> None:
    """
    [TAMBAHAN] Helper untuk handle db calls yang bisa sync ATAU async.
    registry.py dipanggil dari konteks sync (set_profile_override,
    revert_parameter_override, dll) tapi DatabaseManager menggunakan
    async methods. Tanpa penanganan ini, coroutine dari async DB call
    tidak pernah dieksekusi — perubahan tidak tersimpan ke DB secara
    diam-diam tanpa error.

    Solusi: kalau ada running event loop, schedule via create_task.
    Kalau tidak ada running loop (konteks sync murni), tutup coroutine
    agar tidak memory leak dan log debug.
    Konsisten dengan cara apply_parameter_override sudah melakukannya.
    """
    if coro_or_result is None:
        return
    if not hasattr(coro_or_result, "__await__"):
        return  # sudah sync result
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro_or_result)
    except RuntimeError:
        try:
            coro_or_result.close()
        except Exception:
            pass
        log.debug(
            "DB call tidak bisa di-schedule (no running loop) — "
            "perubahan mungkin tidak tersimpan ke DB."
        )


_COIN_PROFILE_MAP: Dict[str, str] = {
    # [BUG-FIX] BTC hilang dari map akibat str_replace error saat audit
    "BTC": "hodl_accumulate",
    "ETH": "hodl_accumulate",
    "SOL":  "breakout_swift",
    "BNB":  "trend_follow",
    "AVAX": "trend_follow",
    "XRP":  "breakout_swift",
    "ADA":  "breakout_swift",
    "DOT":  "breakout_swift",
    "LINK": "breakout_swift",
    "ATOM": "breakout_swift",
    "LTC":  "breakout_swift",
    "NEAR": "scalp_volatile",
    "APT":  "scalp_volatile",
    "SUI":  "scalp_volatile",
    "FET":  "scalp_volatile",
    "INJ":  "scalp_volatile",
    "OP":   "scalp_volatile",
    "ARB":  "scalp_volatile",
    "AIGENSYN": "scalp_volatile",
    "BIO":  "scalp_volatile",
    "HYPER":"scalp_volatile",
    "UNI":  "mean_revert",
    "AAVE": "mean_revert",
    "SNX":  "mean_revert",
    "SPK":  "mean_revert",
    "PEPE":  "extreme_momentum",
    "POL":   "extreme_momentum",
    "DOGE":  "extreme_momentum",
    "SHIB":  "extreme_momentum",
    "FLOKI": "extreme_momentum",
    "WIF":   "extreme_momentum",
    "BONK":  "extreme_momentum",
}

_LEGACY_PROFILE_ALIAS: Dict[str, str] = {
    "hodl":        "hodl_accumulate",
    "trend":       "trend_follow",
    "breakout":    "breakout_swift",
    "scalp":       "scalp_volatile",
    "mean_revert": "mean_revert",
    "extreme":     "extreme_momentum",
    # v6.0 names
    "long_term":  "trend_follow",
    "short_term": "breakout_swift",
}

def _build_coin_profile(symbol: str, profile_name: str, overrides: Optional[Dict[str, Any]] = None) -> CoinProfile:
    thresh = get_profile_thresholds(profile_name)
    kwargs = thresh.to_coinprofile_kwargs()

    timeframe = PROFILE_TIMEFRAME.get(profile_name, "1h")
    timeframe_conf = TIMEFRAME_CONFIRMATION_MAP.get(timeframe, "4h")

    if overrides:
        for param, value in overrides.items():
            if param in kwargs:
                if thresh.param_bounds.is_within_bounds(param, value):
                    kwargs[param] = value
                    log.info(
                        "Applied override: %s/%s | %s = %s",
                        symbol, profile_name, param, value,
                    )
                else:
                    bounds = thresh.param_bounds.get_bounds(param)
                    log.warning(
                        "Override '%s' = %s di luar bounds %s untuk %s/%s — diabaikan.",
                        param, value, bounds, symbol, profile_name,
                    )

    profile_enum = StrategyProfile(profile_name)

    coin_profile = CoinProfile(
        symbol=symbol,
        profile=profile_enum,
        timeframe=timeframe,
        timeframe_conf=timeframe_conf,
        description=f"{PROFILE_EMOJI.get(profile_name, '⚙️')} {profile_name} | {symbol}",
        **kwargs,
    )

    errors = coin_profile.validate()
    if errors:
        log.error(
            "CoinProfile %s/%s validation errors: %s",
            symbol, profile_name, errors,
        )

    return coin_profile

def _build_conservative_profile(symbol: str) -> CoinProfile:
    thresh = get_profile_thresholds("scalp_volatile")
    kwargs = thresh.to_coinprofile_kwargs()

    kwargs.update({
        "volume_mult":       3.0,
        "volume_spike":      5.0,
        "rsi_min":           55.0,
        "rsi_max":           65.0,
        "rsi_gc_min":        55.0,
        "min_breakout_pct":  0.25,
        "atr_sl_mult":       1.5,
        "atr_tp_mult":       2.5,
        "quick_sl_pct":      1.5,
        "quick_tp_pct":      2.5,
        "trailing_act_pct":  2.0,
        "trailing_gap_pct":  0.8,
        "max_hold_candles":  12,
        "entry_threshold":   75.0,
        "typical_atr_pct":   0.8,
    })

    return CoinProfile(
        symbol=symbol,
        profile=StrategyProfile.SCALP_VOLATILE,
        timeframe="1h",
        timeframe_conf="4h",
        description=f"⚠️ Conservative fallback | {symbol} (tidak terdaftar)",
        notes="Tambahkan ke _COIN_PROFILE_MAP untuk konfigurasi optimal.",
        **kwargs,
    )

_ACTIVE_OVERRIDES: Dict[str, Dict[str, Any]] = {}

_MANUAL_PROFILE_OVERRIDES: Dict[str, str] = {}

_PROFILE_CACHE: Dict[str, CoinProfile] = {}

def _invalidate_cache(symbol: Optional[str] = None) -> None:
    if symbol:
        base = symbol.split("/")[0].upper()
        _PROFILE_CACHE.pop(base, None)
    else:
        _PROFILE_CACHE.clear()


def select_profile_from_indicators(
    symbol: str,
    ind_volatility: float = 50.0,
    ind_momentum:   float = 50.0,
    ind_trend:      float = 50.0,
    ema_stack_score: float = 50.0,
    adx:            float = 20.0,
    rsi:            float = 50.0,
    atr_pct:        float = 0.5,
    regime:         str   = "trending_bull",
) -> str:
    """
    Pilih profile otomatis berdasarkan kondisi indikator saat itu.
    Koin tetap punya jati diri (base profile dari _COIN_PROFILE_MAP)
    tapi bisa naik/turun profile sesuai kondisi lapangan.
    """
    base = symbol.split("/")[0].upper()
    base_profile = _COIN_PROFILE_MAP.get(base, "scalp_volatile")

    # Kondisi extreme_momentum — semua koin bisa masuk kalau syarat terpenuhi
    if (
        ema_stack_score >= 88
        and ind_trend    >= 80
        and ind_momentum >= 78
        and adx          >= 30
        and rsi          >= 60
    ):
        return "extreme_momentum"

    # Kondisi trend_follow — tren kuat, volatilitas normal
    if (
        ind_trend    >= 75
        and ema_stack_score >= 75
        and adx          >= 25
        and ind_volatility <= 70
        and regime == "trending_bull"
    ):
        return "trend_follow"

    # Kondisi breakout_swift — momentum tinggi + volatilitas sedang
    if (
        ind_momentum >= 70
        and ind_trend >= 65
        and adx       >= 22
        and atr_pct   >= 0.4
    ):
        return "breakout_swift"

    # Kondisi scalp_volatile — volatilitas tinggi
    if (
        ind_volatility >= 72
        or atr_pct      >= 1.2
        or regime == "volatile_expansion"
    ):
        return "scalp_volatile"

    # Kondisi mean_revert — pasar ranging, momentum lemah
    if (
        regime == "ranging"
        or (adx <= 20 and ind_momentum <= 45)
    ):
        return "mean_revert"

    # Kondisi hodl_accumulate — tren lemah, semua indikator netral
    if (
        ind_trend    <= 55
        and ind_momentum <= 50
        and adx          <= 20
    ):
        return "hodl_accumulate"

    # Fallback — pakai jati diri koin
    return base_profile

def get_coin_profile(
    symbol: str,
    override_profile: Optional[str] = None,
    db_manager=None,
) -> CoinProfile:

    base = symbol.split("/")[0].upper()

    if db_manager is not None and base not in _ACTIVE_OVERRIDES:
        _load_overrides_from_db(base, db_manager)

    if override_profile is None and base in _PROFILE_CACHE:
        return _PROFILE_CACHE[base]

    if override_profile:
        profile_name = _resolve_profile_name(override_profile)
        if profile_name is None:
            log.warning(
                "get_coin_profile: override_profile '%s' tidak dikenal, diabaikan.",
                override_profile,
            )
            profile_name = _get_default_profile_name(base)
    elif base in _MANUAL_PROFILE_OVERRIDES:
        profile_name = _MANUAL_PROFILE_OVERRIDES[base]
    else:
        profile_name = _get_default_profile_name(base)

    param_overrides = _ACTIVE_OVERRIDES.get(base, {})

    if profile_name is None:
        # Cek apakah auto_classify sudah cache di _COIN_PROFILE_MAP runtime
        auto_name = _COIN_PROFILE_MAP.get(base)
        if auto_name:
            profile_name = auto_name
            profile = _build_coin_profile(symbol, profile_name, param_overrides)
            log.info("AutoClassify (cached): %s → %s", base, profile_name)
        else:
            log.warning(
                "Coin '%s' tidak ada di _COIN_PROFILE_MAP — menggunakan profil konservatif.",
                base,
            )
            profile = _build_conservative_profile(symbol)
    else:
        profile = _build_coin_profile(symbol, profile_name, param_overrides)

    if override_profile is None:
        _PROFILE_CACHE[base] = profile

    return profile

def _get_default_profile_name(base: str) -> Optional[str]:
    return _COIN_PROFILE_MAP.get(base)

def auto_classify_profile(base: str, ticker: dict, spread_pct: float = 0.0) -> str:
    """
    Klasifikasi otomatis profil koin berdasarkan data live ticker.
    Dipakai sebagai fallback kalau koin tidak ada di _COIN_PROFILE_MAP.

    Parameter:
        base       : simbol base coin (misal "SOL", "BONK")
        ticker     : dict dari ws_feed.live_tickers[symbol]
        spread_pct : spread % dari ws_feed.get_spread_pct(symbol)

    Return:
        nama profil (str) — salah satu dari StrategyProfile values
    """
    try:
        quote_volume = float(ticker.get("quote_volume") or 0)
        last         = float(ticker.get("last") or 0)
        # Fallback volume dari base_volume * price
        if quote_volume <= 0 and last > 0:
            quote_volume = float(ticker.get("volume") or 0) * last
    except Exception:
        quote_volume = 0.0

    spread = abs(spread_pct) if spread_pct else 0.0

    # ── Klasifikasi berdasarkan volume + spread ──────────────────
    # Tier 1: Blue chip — volume sangat tinggi, spread sangat kecil
    if quote_volume >= 50_000_000 and spread < 0.15:
        profile = "hodl_accumulate"

    # Tier 2: Mid cap stabil — volume tinggi, spread kecil
    elif quote_volume >= 10_000_000 and spread < 0.25:
        profile = "trend_follow"

    # Tier 3: Liquid aktif — volume cukup, spread wajar
    elif quote_volume >= 3_000_000 and spread < 0.40:
        profile = "breakout_swift"

    # Tier 4: Volatile liquid — volume cukup tapi spread lebar
    elif quote_volume >= 1_000_000 and spread < 0.80:
        profile = "scalp_volatile"

    # Tier 5: Meme/pump — volume cukup tapi spread sangat lebar
    elif quote_volume >= 500_000 and spread >= 0.80:
        profile = "extreme_momentum"

    # Tier 6: Low volume — cenderung ranging, mean revert
    elif quote_volume >= 100_000:
        profile = "mean_revert"

    # Tier 7: Sangat low volume — pakai mean_revert (paling jarang trade, paling aman)
    else:
        profile = "mean_revert"

    # Cache hasil ke _COIN_PROFILE_MAP runtime (tidak ubah file)
    _COIN_PROFILE_MAP[base] = profile
    log.info(
        "AutoClassify: %s → %s | vol_24h=$%.0f spread=%.3f%%",
        base, profile, quote_volume, spread,
    )
    return profile

def _resolve_profile_name(name: str) -> Optional[str]:
    if not name:
        return None
    lower = name.lower()

    known = get_all_profile_names()
    if lower in known:
        return lower

    if lower in _LEGACY_PROFILE_ALIAS:
        return _LEGACY_PROFILE_ALIAS[lower]

    for prof in StrategyProfile:
        if prof.value == lower:
            return prof.value

    return None

def set_profile_override(
    symbol: str,
    profile_name: str,
    db_manager=None,
) -> Tuple[bool, str]:
    resolved = _resolve_profile_name(profile_name)
    if resolved is None:
        valid = get_all_profile_names() + list(_LEGACY_PROFILE_ALIAS.keys())
        return False, (
            f"Profile '{profile_name}' tidak dikenal. "
            f"Valid: {sorted(set(valid))}"
        )

    base = symbol.split("/")[0].upper()
    _MANUAL_PROFILE_OVERRIDES[base] = resolved
    _invalidate_cache(base)

    if db_manager is not None:
        try:
            # [BUG-FIX] Sebelumnya: save_parameter_change dipanggil tanpa
            # penanganan async — DatabaseManager.save_parameter_change adalah
            # async method, tanpa await/create_task coroutine tidak pernah
            # dieksekusi, override tidak tersimpan ke DB secara diam-diam.
            # Sekarang: pakai _fire_and_forget_db yang konsisten dengan cara
            # apply_parameter_override sudah melakukannya.
            _fire_and_forget_db(db_manager.save_parameter_change(
                symbol=base,
                profile=resolved,
                parameter_name="_profile_override",
                old_value=_COIN_PROFILE_MAP.get(base, "unknown"),
                new_value=resolved,
                reason="Manual profile override via set_profile_override()",
                approved_by="manual",
            ))
        except Exception as exc:
            log.warning("Gagal simpan profile override ke DB: %s", exc)

    log.info("Profile override set: %s → %s", base, resolved)
    profile = get_coin_profile(symbol)
    return True, (
        f"Override berhasil: {symbol} → {resolved} "
        f"(TF={profile.timeframe}, threshold={profile.entry_threshold})"
    )

def clear_profile_override(symbol: str, db_manager=None) -> bool:
    base = symbol.split("/")[0].upper()
    existed = base in _MANUAL_PROFILE_OVERRIDES

    if existed:
        del _MANUAL_PROFILE_OVERRIDES[base]
        _invalidate_cache(base)
        log.info("Profile override cleared: %s", base)

        if db_manager is not None:
            try:
                # [BUG-FIX] Sama seperti set_profile_override — async call
                # tanpa penanganan yang benar. Fix: _fire_and_forget_db.
                _fire_and_forget_db(db_manager.save_parameter_change(
                    symbol=base,
                    profile=_COIN_PROFILE_MAP.get(base, "unknown"),
                    parameter_name="_profile_override",
                    old_value=_MANUAL_PROFILE_OVERRIDES.get(base, "override"),
                    new_value="(cleared)",
                    reason="Profile override cleared",
                    approved_by="manual",
                ))
            except Exception as exc:
                log.warning("Gagal simpan clear override ke DB: %s", exc)

    return existed

def apply_parameter_override(
    symbol: str,
    profile_name: str,
    parameter_name: str,
    new_value: Any,
    source: str = "meta_learner",
    db_manager=None,
) -> Tuple[bool, str]:
    base = symbol.split("/")[0].upper()

    profile_name_resolved = (
        _MANUAL_PROFILE_OVERRIDES.get(base)
        or _COIN_PROFILE_MAP.get(base)
    )
    if profile_name_resolved is None:
        return False, f"Coin '{base}' tidak terdaftar, tidak bisa apply parameter override."

    thresh = get_profile_thresholds(profile_name_resolved)
    if not thresh.param_bounds.is_within_bounds(parameter_name, new_value):
        bounds = thresh.param_bounds.get_bounds(parameter_name)
        return False, (
            f"Parameter '{parameter_name}' = {new_value} di luar bounds {bounds} "
            f"untuk {base}/{profile_name_resolved}."
        )

    old_profile = get_coin_profile(symbol)
    old_value = getattr(old_profile, parameter_name, None)

    if base not in _ACTIVE_OVERRIDES:
        _ACTIVE_OVERRIDES[base] = {}
    _ACTIVE_OVERRIDES[base][parameter_name] = new_value
    _invalidate_cache(base)

    if db_manager is not None:
        try:
            res = db_manager.save_parameter_change(
                symbol=base,
                profile=profile_name_resolved,
                parameter_name=parameter_name,
                old_value=old_value,
                new_value=new_value,
                reason=f"Applied by {source}",
                approved_by=source,
            )
            if hasattr(res, "__await__"):
                try:
                    asyncio.get_running_loop().create_task(res)
                except RuntimeError:
                    # No running loop; best-effort fire-and-forget.
                    pass
        except Exception as exc:
            log.warning("Gagal simpan parameter override ke DB: %s", exc)

    log.info(
        "Parameter override applied: %s/%s | %s: %s → %s (source=%s)",
        base, profile_name_resolved, parameter_name, old_value, new_value, source,
    )
    return True, (
        f"Override applied: {base}/{profile_name_resolved} | "
        f"{parameter_name}: {old_value} → {new_value}"
    )

def revert_parameter_override(
    symbol: str,
    parameter_name: str,
    db_manager=None,
) -> Tuple[bool, str]:
    base = symbol.split("/")[0].upper()

    if base not in _ACTIVE_OVERRIDES or parameter_name not in _ACTIVE_OVERRIDES[base]:
        return False, f"Tidak ada override aktif untuk {base}/{parameter_name}."

    old_override = _ACTIVE_OVERRIDES[base].pop(parameter_name)
    if not _ACTIVE_OVERRIDES[base]:
        del _ACTIVE_OVERRIDES[base]

    _invalidate_cache(base)

    profile_name_resolved = _COIN_PROFILE_MAP.get(base, "unknown")
    if db_manager is not None:
        try:
            # [BUG-FIX] Sama — async call tanpa penanganan. Fix: _fire_and_forget_db.
            _fire_and_forget_db(db_manager.save_parameter_change(
                symbol=base,
                profile=profile_name_resolved,
                parameter_name=parameter_name,
                old_value=old_override,
                new_value="(reverted to default)",
                reason="Reverted due to performance degradation",
                approved_by="meta_learner",
            ))
        except Exception as exc:
            log.warning("Gagal simpan revert ke DB: %s", exc)

    log.info(
        "Parameter override reverted: %s/%s | %s (was: %s)",
        base, profile_name_resolved, parameter_name, old_override,
    )
    return True, (
        f"Reverted: {base}/{profile_name_resolved} | "
        f"{parameter_name} kembali ke default."
    )

def _load_overrides_from_db(symbol: str, db_manager) -> None:
    try:
        history = db_manager.get_parameter_history(symbol=symbol, limit=100)
        if not history:
            return

        applied: Dict[str, Any] = {}
        for record in sorted(history, key=lambda r: r.get("timestamp", ""), reverse=True):
            param = record.get("parameter_name", "")
            if param.startswith("_"):
                continue
            if param not in applied:
                new_val = record.get("new_value")
                if new_val is not None and new_val != "(reverted to default)":
                    applied[param] = new_val

        if applied:
            _ACTIVE_OVERRIDES[symbol] = applied
            log.debug(
                "Loaded %d parameter overrides dari DB untuk %s: %s",
                len(applied), symbol, list(applied.keys()),
            )

    except Exception as exc:
        log.warning("Gagal load overrides dari DB untuk %s: %s", symbol, exc)

def load_all_overrides_from_db(db_manager) -> int:
    count = 0
    all_symbols = list(_COIN_PROFILE_MAP.keys())

    for base in all_symbols:
        try:
            history = db_manager.get_parameter_history(symbol=base, limit=100)
            if not history:
                continue

            for record in sorted(history, key=lambda r: r.get("timestamp", ""), reverse=True):
                param = record.get("parameter_name", "")
                if param == "_profile_override":
                    new_val = record.get("new_value", "")
                    if new_val and new_val != "(cleared)":
                        _MANUAL_PROFILE_OVERRIDES[base] = new_val
                    break

            applied: Dict[str, Any] = {}
            seen: set = set()
            for record in sorted(history, key=lambda r: r.get("timestamp", ""), reverse=True):
                param = record.get("parameter_name", "")
                if param.startswith("_") or param in seen:
                    continue
                seen.add(param)
                new_val = record.get("new_value")
                if new_val is not None and new_val != "(reverted to default)":
                    applied[param] = new_val

            if applied:
                _ACTIVE_OVERRIDES[base] = applied
                count += 1

        except Exception as exc:
            log.warning("Gagal load overrides untuk %s: %s", base, exc)

    _invalidate_cache()
    log.info(
        "Loaded overrides dari DB: %d symbols punya active parameter overrides. "
        "%d symbols punya manual profile override.",
        count, len(_MANUAL_PROFILE_OVERRIDES),
    )
    return count

def get_all_overrides() -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for base, params in _ACTIVE_OVERRIDES.items():
        result[base] = dict(params)
    for base, profile_name in _MANUAL_PROFILE_OVERRIDES.items():
        if base not in result:
            result[base] = {}
        result[base]["_profile"] = profile_name
    return result

def get_profile_summary(symbols: List[str]) -> str:
    lines = ["📊 Strategy Profile Assignments (v7.0):"]
    by_profile: Dict[str, List[str]] = {}

    for sym in symbols:
        prof = get_coin_profile(sym)
        by_profile.setdefault(prof.profile.value, []).append(sym.split("/")[0])

    for pname, coins in sorted(by_profile.items()):
        emoji = PROFILE_EMOJI.get(pname, "⚙️")
        tf = PROFILE_TIMEFRAME.get(pname, "?")
        lines.append(f"  {emoji} {pname:<22} ({tf}): {', '.join(coins)}")

    overridden = [b for b in _MANUAL_PROFILE_OVERRIDES]
    if overridden:
        lines.append(f"\n  ✏️ Manual profile overrides: {', '.join(overridden)}")

    overridden_params = [b for b in _ACTIVE_OVERRIDES]
    if overridden_params:
        lines.append(f"  🔧 Parameter overrides aktif: {', '.join(overridden_params)}")

    return "\n".join(lines)

def get_all_registered_symbols() -> List[str]:
    return list(_COIN_PROFILE_MAP.keys())

def is_registered(symbol: str) -> bool:
    base = symbol.split("/")[0].upper()
    return base in _COIN_PROFILE_MAP or base in _MANUAL_PROFILE_OVERRIDES

def register_coin(
    symbol: str,
    profile_name: str,
    persist: bool = False,
    db_manager=None,
) -> Tuple[bool, str]:
    resolved = _resolve_profile_name(profile_name)
    if resolved is None:
        return False, f"Profile '{profile_name}' tidak dikenal."

    base = symbol.split("/")[0].upper()

    _COIN_PROFILE_MAP[base] = resolved
    _invalidate_cache(base)

    if persist and db_manager:
        return set_profile_override(base, resolved, db_manager=db_manager)

    log.info("Coin registered (runtime): %s → %s", base, resolved)
    return True, f"Coin {base} registered dengan profile {resolved}."
