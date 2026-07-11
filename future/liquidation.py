"""
future/liquidation.py — Kalkulasi Liquidation Price untuk Binance USDT-M Futures

╔══════════════════════════════════════════════════════════════════════════╗
║  ⚠️  PERINGATAN KERAS — WAJIB DIBACA SEBELUM DIPAKAI UNTUK TRADING NYATA  ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Formula di modul ini adalah formula SIMPLIFIED/APPROXIMATE yang umum     ║
║  dipublikasikan dan dipakai banyak tool trading pihak ketiga. Formula ini ║
║  BELUM diverifikasi langsung terhadap kalkulator resmi Binance saat modul ║
║  ini ditulis.                                                             ║
║                                                                            ║
║  Binance sebenarnya memakai sistem TIERED MAINTENANCE MARGIN BRACKET      ║
║  (maintenance margin rate DAN "maintenance amount" deduction berbeda-beda ║
║  tergantung notional value posisi) -- formula simplified di sini          ║
║  mengasumsikan SATU nilai MMR konstan, yang HANYA akurat untuk posisi     ║
║  kecil di tier margin terendah. Untuk posisi lebih besar yang masuk ke    ║
║  tier lebih tinggi, hasil kalkulasi di sini BISA MELESET dari liquidation ║
║  price asli di exchange.                                                  ║
║                                                                            ║
║  SEBELUM dipakai untuk keputusan finansial nyata (bahkan paper trading    ║
║  yang mengklaim akurat), WAJIB:                                          ║
║  1. Cross-check hasil kalkulasi di sini dengan Liquidation Price          ║
║     Calculator resmi Binance (futures.binance.com), untuk beberapa        ║
║     kombinasi leverage/notional yang representatif.                      ║
║  2. Pertimbangkan pakai `exchange.fetch_leverage_tiers(symbol)` (tersedia ║
║     di ccxt) untuk ambil data bracket margin ASLI per simbol, alih-alih   ║
║     MMR konstan yang dipakai di sini sebagai pendekatan.                  ║
║  3. JANGAN PERNAH mengandalkan modul ini sebagai SATU-SATUNYA safety net  ║
║     -- selalu pasang stop-loss yang scara matematis BERADA SEBELUM       ║
║     (lebih longgar dari) liquidation price hasil kalkulasi di sini,       ║
║     dengan margin keamanan tambahan yang signifikan.                     ║
╚══════════════════════════════════════════════════════════════════════════╝

Referensi konsep umum (isolated margin, tanpa funding fee, tanpa fee taker/maker):

  LONG:  Liq Price = Entry Price × (1 + MMR - 1/Leverage)
  SHORT: Liq Price = Entry Price × (1 - MMR + 1/Leverage)

  Dimana MMR (Maintenance Margin Rate) adalah rasio margin minimum yang
  harus dipertahankan, bervariasi per simbol dan per tier notional value
  (Binance mempublikasikan tabel tier ini per simbol, cek fetch_leverage_tiers).

  Default MMR yang dipakai di sini (0.005 = 0.5%) adalah nilai umum untuk
  tier notional TERENDAH banyak pair populer di Binance USDT-M Futures per
  pengetahuan umum penulis -- TAPI ini bisa berbeda per simbol dan bisa
  berubah sewaktu-waktu. JANGAN anggap ini universal tanpa verifikasi.
"""

from dataclasses import dataclass
from typing import Optional


DEFAULT_MMR = 0.005  # 0.5% -- APPROXIMATE, lihat peringatan di atas


@dataclass
class LiquidationResult:
    liquidation_price: float
    distance_pct: float          # jarak dari entry ke liquidation, dalam %
    margin_used: float           # margin awal (isolated) yang dipakai
    is_estimate: bool = True     # selalu True -- lihat peringatan modul ini


def calculate_liquidation_price(
    entry_price: float,
    leverage: float,
    side: str = "long",
    mmr: float = DEFAULT_MMR,
    margin_mode: str = "isolated",
) -> LiquidationResult:
    """
    Hitung estimasi liquidation price untuk posisi isolated margin.

    PERINGATAN: lihat docstring modul ini -- ini APPROXIMATE, bukan presisi
    penuh terhadap sistem tiered margin bracket asli Binance.

    Args:
        entry_price: harga entry posisi
        leverage: leverage yang dipakai (misal 10 untuk 10x)
        side: "long" atau "short"
        mmr: Maintenance Margin Rate (default 0.005 = 0.5%, APPROXIMATE)
        margin_mode: "isolated" (didukung) atau "cross" (BELUM diimplementasi
                     dengan benar -- cross margin melibatkan seluruh saldo
                     akun, jauh lebih kompleks, raise NotImplementedError)

    Returns:
        LiquidationResult dengan liquidation_price, jarak %, dan margin yang dipakai.

    Raises:
        ValueError: kalau parameter tidak valid (leverage<=0, entry_price<=0, dst)
        NotImplementedError: kalau margin_mode="cross" (belum didukung)
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price harus > 0, dapat: {entry_price}")
    if leverage <= 0:
        raise ValueError(f"leverage harus > 0, dapat: {leverage}")
    if mmr <= 0 or mmr >= 1:
        raise ValueError(f"mmr harus di antara 0 dan 1, dapat: {mmr}")
    if side not in ("long", "short"):
        raise ValueError(f"side harus 'long' atau 'short', dapat: {side!r}")

    if margin_mode == "cross":
        raise NotImplementedError(
            "Cross margin liquidation price melibatkan SELURUH saldo akun "
            "(bukan cuma margin posisi ini), jauh lebih kompleks untuk dihitung "
            "akurat dan BELUM diimplementasikan dengan benar di modul ini. "
            "Gunakan margin_mode='isolated' untuk saat ini."
        )
    if margin_mode != "isolated":
        raise ValueError(f"margin_mode harus 'isolated' atau 'cross', dapat: {margin_mode!r}")

    inv_leverage = 1.0 / leverage

    if side == "long":
        liq_price = entry_price * (1 + mmr - inv_leverage)
    else:  # short
        liq_price = entry_price * (1 - mmr + inv_leverage)

    if liq_price <= 0:
        # Bisa terjadi kalau leverage sangat tinggi utk short (secara teori
        # liq price short seharusnya selalu positif untuk leverage wajar,
        # tapi jaga-jaga edge case leverage ekstrem/mmr besar)
        raise ValueError(
            f"Kalkulasi menghasilkan liquidation_price <= 0 ({liq_price:.8f}) -- "
            f"kombinasi leverage={leverage}x dan mmr={mmr} tidak masuk akal. "
            f"Periksa parameter input."
        )

    distance_pct = abs(liq_price - entry_price) / entry_price * 100.0
    margin_used = entry_price / leverage  # margin per unit posisi (isolated)

    return LiquidationResult(
        liquidation_price=round(liq_price, 8),
        distance_pct=round(distance_pct, 4),
        margin_used=round(margin_used, 8),
        is_estimate=True,
    )


def is_stop_loss_safe(
    stop_loss_price: float,
    liquidation_price: float,
    side: str = "long",
    min_safety_margin_pct: float = 20.0,
) -> bool:
    """
    Cek apakah stop_loss_price memberi jarak aman yang cukup SEBELUM
    liquidation_price tersentuh -- yaitu prasyarat keamanan #3 di peringatan
    modul ini: SL harus lebih longgar dari liquidation price, dengan margin
    keamanan tambahan (default 20% dari jarak entry-ke-liquidation).

    Return True kalau SL aman (tidak akan pernah memicu liquidation duluan
    sebelum SL sempat jalan, DENGAN asumsi liquidation_price yang dihitung
    itu sendiri akurat -- lihat peringatan modul ini soal akurasi).
    """
    if side == "long":
        # Long: liquidation di bawah entry, SL juga di bawah entry.
        # SL aman kalau SL > liquidation_price (SL kena duluan sebelum liq),
        # DENGAN margin tambahan.
        if stop_loss_price <= liquidation_price:
            return False
        gap_pct = (stop_loss_price - liquidation_price) / liquidation_price * 100.0
        return gap_pct >= min_safety_margin_pct
    else:  # short
        if stop_loss_price >= liquidation_price:
            return False
        gap_pct = (liquidation_price - stop_loss_price) / liquidation_price * 100.0
        return gap_pct >= min_safety_margin_pct
