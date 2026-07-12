"""
future/funding.py — Kalkulasi dampak Funding Rate ke PnL posisi futures

Funding rate adalah biaya periodik (biasanya tiap 8 jam di Binance USDT-M
Futures) yang dibayar/diterima antara pihak long dan short, tergantung tanda
funding rate saat itu. Ini TIDAK ADA di spot sama sekali -- exclusive utk
futures/perpetual.

Mekanisme (Binance USDT-M Futures):
- funding_rate POSITIF -> LONG membayar ke SHORT
- funding_rate NEGATIF -> SHORT membayar ke LONG
- Dibayar berdasar notional value posisi (amount * mark_price), BUKAN margin
- Terjadi tiap funding interval (biasanya 00:00, 08:00, 16:00 UTC di Binance)

⚠️ CATATAN: funding_rate berubah-ubah tiap interval (ditentukan pasar,
bukan konstan) -- modul ini cuma menghitung DAMPAK dari satu nilai funding
rate yang diberikan (biasanya hasil fetch_funding_rate() dari exchange),
bukan memprediksi funding rate masa depan.
"""

from dataclasses import dataclass
from typing import List
from datetime import datetime


@dataclass
class FundingPayment:
    timestamp:    datetime
    symbol:       str
    funding_rate: float
    notional:     float
    payment:      float   # negatif = posisi MEMBAYAR, positif = posisi MENERIMA


def calculate_funding_payment(
    notional_value: float,
    funding_rate:   float,
    side:           str,
) -> float:
    """
    Hitung satu kali pembayaran funding untuk posisi dengan notional_value
    dan funding_rate tertentu.

    Return: payment (negatif = posisi ini MEMBAYAR funding, positif = MENERIMA)

    Mekanisme Binance: funding_rate positif -> long membayar short.
        - LONG:  payment = -notional_value * funding_rate  (bayar kalau rate>0)
        - SHORT: payment = +notional_value * funding_rate  (terima kalau rate>0)
    """
    if side not in ("long", "short"):
        raise ValueError(f"side harus 'long' atau 'short', dapat: {side!r}")
    if notional_value < 0:
        raise ValueError(f"notional_value tidak boleh negatif: {notional_value}")

    if side == "long":
        return round(-notional_value * funding_rate, 8)
    else:
        return round(notional_value * funding_rate, 8)


def project_funding_cost(
    notional_value:     float,
    side:               str,
    expected_funding_rate: float,
    hold_hours:         float,
    funding_interval_hours: float = 8.0,
) -> float:
    """
    Estimasi TOTAL biaya/penerimaan funding kalau posisi ditahan selama
    hold_hours, DENGAN ASUMSI funding_rate tetap sama sepanjang waktu itu
    (asumsi yang SANGAT disederhanakan -- funding rate riil berubah tiap
    interval, ini cuma estimasi kasar utk perencanaan, bukan angka pasti).

    Berguna utk pertimbangan: "kalau nahan posisi 24 jam dengan funding rate
    saat ini X%, kira-kira biaya funding-nya berapa?"
    """
    if hold_hours < 0:
        raise ValueError(f"hold_hours tidak boleh negatif: {hold_hours}")
    if funding_interval_hours <= 0:
        raise ValueError(f"funding_interval_hours harus > 0: {funding_interval_hours}")

    num_intervals = hold_hours / funding_interval_hours
    single_payment = calculate_funding_payment(notional_value, expected_funding_rate, side)
    return round(single_payment * num_intervals, 8)


def summarize_funding_history(payments: List[FundingPayment]) -> dict:
    """Ringkasan total funding yang sudah dibayar/diterima dari histori payment."""
    total = sum(p.payment for p in payments)
    paid = sum(p.payment for p in payments if p.payment < 0)
    received = sum(p.payment for p in payments if p.payment > 0)
    return {
        "total_net":      round(total, 8),
        "total_paid":     round(abs(paid), 8),
        "total_received": round(received, 8),
        "num_payments":   len(payments),
    }
