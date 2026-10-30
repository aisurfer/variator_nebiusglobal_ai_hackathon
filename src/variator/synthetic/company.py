"""Deterministic NumPy simulation of a fictional mid-size online distributor.

One seed produces one coherent company history (36 months, 2023-11 .. 2026-10).
All demo cases are cut from the same history, so the warehouse, pricing and
new-market stories describe the same business.

Calibration targets (consistent with ``variator.domain.examples.warehouse_decision``):
~400k orders in the last 12 months, ~12% yearly growth, contribution margin
before fulfilment ~$9/order, in-house fulfilment ~$3/order, and a warehouse
running close to capacity with overflow in peak months.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np

COMPANY_NAME = "Northbeam Home & Garden Supply (fictional)"

START_YEAR = 2023
START_MONTH = 11
N_MONTHS = 36

REGIONS: tuple[str, ...] = ("north", "south", "east", "west")
CATEGORIES: tuple[str, ...] = ("home", "garden", "kitchen", "pet")

TARGET_LAST12_ORDERS = 400_000

# Noise levels: one market-wide shock per month plus independent cell noise.
COMMON_SIGMA = 0.015
CELL_SIGMA = 0.03

COST_INFLATION = 0.045  # yearly supplier + inbound freight inflation on COGS
FULFILMENT_BASE = 2.55  # in-house cost per order at 2023-11, USD
FULFILMENT_INFLATION = 0.025
CONGESTION_COEF = 1.2  # extra cost share per unit of utilization above 0.85
SPOT_3PL_BASE = 4.30  # ad-hoc overflow fee per order at 2023-11, USD
MARKETING_PER_ORDER = 1.45
ORDERS_PER_ACTIVE_CUSTOMER = 0.5  # monthly
BASE_CHURN = 0.034

CAPACITY_BEFORE = 33_000  # orders/month until the 2024-07 racking upgrade
CAPACITY_AFTER = 35_000
CAPACITY_UPGRADE_MONTH = "2024-07"


@dataclass(frozen=True)
class RegionProfile:
    share: float  # share of orders at 2023-11
    annual_growth: float
    delivery_days: float
    households_k: float  # addressable households, thousands


@dataclass(frozen=True)
class CategoryProfile:
    share: float
    base_aov: float  # average order value at 2023-11, USD
    cogs_ratio: float  # COGS / AOV at 2023-11
    elasticity: float  # true own-price elasticity of order volume
    seasonality: tuple[float, ...]  # Jan..Dec, normalized to mean 1 at use


REGION_PROFILES: dict[str, RegionProfile] = {
    "north": RegionProfile(share=0.31, annual_growth=0.14, delivery_days=2.1, households_k=520.0),
    "south": RegionProfile(share=0.20, annual_growth=0.32, delivery_days=3.2, households_k=470.0),
    "east": RegionProfile(share=0.29, annual_growth=0.15, delivery_days=2.4, households_k=480.0),
    "west": RegionProfile(share=0.20, annual_growth=0.16, delivery_days=2.9, households_k=390.0),
}

CATEGORY_PROFILES: dict[str, CategoryProfile] = {
    "home": CategoryProfile(
        share=0.30,
        base_aov=40.0,
        cogs_ratio=0.74,
        elasticity=-1.3,
        seasonality=(0.88, 0.86, 0.95, 0.98, 0.98, 0.95, 0.93, 0.97, 1.00, 1.05, 1.18, 1.27),
    ),
    "garden": CategoryProfile(
        share=0.20,
        base_aov=35.0,
        cogs_ratio=0.72,
        elasticity=-1.1,
        seasonality=(0.60, 0.70, 1.05, 1.40, 1.55, 1.40, 1.15, 0.95, 0.85, 0.80, 0.75, 0.80),
    ),
    "kitchen": CategoryProfile(
        share=0.28,
        base_aov=31.0,
        cogs_ratio=0.75,
        elasticity=-1.5,
        seasonality=(0.90, 0.88, 0.95, 0.97, 0.98, 0.95, 0.94, 0.96, 1.00, 1.04, 1.17, 1.26),
    ),
    "pet": CategoryProfile(
        share=0.22,
        base_aov=26.0,
        cogs_ratio=0.78,
        elasticity=-0.9,
        seasonality=(0.98, 0.95, 1.00, 1.00, 1.00, 0.99, 0.99, 1.00, 1.00, 1.01, 1.03, 1.05),
    ),
}


@dataclass(frozen=True)
class PriceChange:
    change_id: str
    month: str  # effective month, YYYY-MM
    category: str  # a category name or "all"
    pct: float
    competitor_response: str


PRICE_CHANGES: tuple[PriceChange, ...] = (
    PriceChange("PC-01", "2024-03", "pet", 0.05, "none observed"),
    PriceChange("PC-02", "2024-09", "kitchen", 0.06, "two competitors matched within 2 months"),
    PriceChange("PC-03", "2025-02", "home", 0.05, "none observed"),
    PriceChange("PC-04", "2025-06", "garden", 0.05, "main competitor ran a 10% promotion"),
    PriceChange("PC-05", "2025-10", "all", 0.04, "partially matched by the market leader"),
    PriceChange("PC-06", "2026-04", "pet", 0.06, "none observed"),
)


def month_label(index: int) -> str:
    """Month index 0 = 2023-11 -> 'YYYY-MM'."""
    total = START_YEAR * 12 + (START_MONTH - 1) + index
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_index(label: str) -> int:
    """'YYYY-MM' -> month index relative to 2023-11."""
    year, month = (int(part) for part in label.split("-"))
    return (year * 12 + month - 1) - (START_YEAR * 12 + START_MONTH - 1)


def derived_rng(seed: int, label: str) -> np.random.Generator:
    """Independent, order-free random stream per component (crc32 is stable across runs)."""
    return np.random.default_rng([seed, zlib.crc32(label.encode())])


@dataclass(frozen=True)
class CompanyHistory:
    """Simulated monthly history. Arrays are indexed [month], [month, region, category] etc."""

    seed: int
    months: tuple[str, ...]
    orders: np.ndarray  # [t, r, c] int
    expected_orders: np.ndarray  # [t, r, c] noiseless, with price effects
    common_shock: np.ndarray  # [t] log-scale market shock
    price_index: np.ndarray  # [t, c] list price index, 1.0 at start
    avg_price: np.ndarray  # [t, r, c] USD per order
    cogs_per_order: np.ndarray  # [t, r, c] USD per order
    capacity: np.ndarray  # [t] orders/month
    inhouse_cost_per_order: np.ndarray  # [t]
    spot_fee_per_order: np.ndarray  # [t]
    marketing_spend: np.ndarray  # [t] USD
    active_customers: np.ndarray  # [t] int
    churn_rate: np.ndarray  # [t]
    churned_customers: np.ndarray  # [t] int
    new_customers: np.ndarray  # [t] int
    overtime_hours: np.ndarray  # [t] int
    on_time_ship_rate: np.ndarray  # [t]

    @property
    def n_months(self) -> int:
        return len(self.months)

    @property
    def total_orders(self) -> np.ndarray:
        return self.orders.sum(axis=(1, 2))

    @property
    def revenue(self) -> np.ndarray:
        """[t, r, c] USD, rounded to cents."""
        return np.round(self.orders * self.avg_price, 2)

    @property
    def cogs(self) -> np.ndarray:
        return np.round(self.orders * self.cogs_per_order, 2)

    @property
    def inhouse_orders(self) -> np.ndarray:
        return np.minimum(self.total_orders, self.capacity)

    @property
    def overflow_orders(self) -> np.ndarray:
        return self.total_orders - self.inhouse_orders

    @property
    def utilization(self) -> np.ndarray:
        return self.inhouse_orders / self.capacity

    @property
    def fulfilment_cost(self) -> np.ndarray:
        return np.round(
            self.inhouse_orders * self.inhouse_cost_per_order
            + self.overflow_orders * self.spot_fee_per_order,
            2,
        )


def _calendar_months(n: int) -> np.ndarray:
    """0-based calendar month (0 = January) for each month index."""
    return (START_MONTH - 1 + np.arange(n)) % 12


def _price_index(n: int) -> np.ndarray:
    index = np.ones((n, len(CATEGORIES)))
    for change in PRICE_CHANGES:
        start = month_index(change.month)
        for c, category in enumerate(CATEGORIES):
            if change.category in ("all", category):
                index[start:, c] *= 1 + change.pct
    return index


def affected_categories(change: PriceChange) -> list[int]:
    return [c for c, name in enumerate(CATEGORIES) if change.category in ("all", name)]


def simulate_company(seed: int = 42) -> CompanyHistory:
    """Run the full company simulation for one seed."""
    rng = derived_rng(seed, "company")
    n, n_r, n_c = N_MONTHS, len(REGIONS), len(CATEGORIES)
    years = np.arange(n) / 12.0
    cal = _calendar_months(n)

    region_trend = np.array(
        [
            [REGION_PROFILES[r].share * (1 + REGION_PROFILES[r].annual_growth) ** y for r in REGIONS]
            for y in years
        ]
    )  # [t, r]
    season = np.array([np.array(CATEGORY_PROFILES[c].seasonality) for c in CATEGORIES])
    season = season / season.mean(axis=1, keepdims=True)  # [c, 12]
    cat_share = np.array([CATEGORY_PROFILES[c].share for c in CATEGORIES])
    elasticity = np.array([CATEGORY_PROFILES[c].elasticity for c in CATEGORIES])

    price_index = _price_index(n)  # [t, c]
    price_effect = price_index**elasticity  # [t, c]

    shape = region_trend[:, :, None] * cat_share[None, None, :] * season[:, cal].T[:, None, :]
    expected = shape * price_effect[:, None, :]
    scale = TARGET_LAST12_ORDERS / expected[-12:].sum()
    expected = expected * scale

    common = rng.normal(0.0, COMMON_SIGMA, n)
    cell = rng.normal(0.0, CELL_SIGMA, (n, n_r, n_c))
    drift = -0.5 * (COMMON_SIGMA**2 + CELL_SIGMA**2)
    orders = np.round(expected * np.exp(common[:, None, None] + cell + drift)).astype(np.int64)

    base_aov = np.array([CATEGORY_PROFILES[c].base_aov for c in CATEGORIES])
    cogs_ratio = np.array([CATEGORY_PROFILES[c].cogs_ratio for c in CATEGORIES])
    avg_price = (
        base_aov[None, None, :] * price_index[:, None, :] * np.exp(rng.normal(0.0, 0.01, (n, n_r, n_c)))
    )
    cogs_per_order = (
        (base_aov * cogs_ratio)[None, None, :]
        * ((1 + COST_INFLATION) ** years)[:, None, None]
        * np.exp(rng.normal(0.0, 0.006, (n, n_r, n_c)))
    )
    avg_price = np.round(avg_price, 2)
    cogs_per_order = np.round(cogs_per_order, 2)

    total = orders.sum(axis=(1, 2))
    capacity = np.where(np.arange(n) < month_index(CAPACITY_UPGRADE_MONTH), CAPACITY_BEFORE, CAPACITY_AFTER)
    utilization = np.minimum(total, capacity) / capacity
    overflow_share = (total - np.minimum(total, capacity)) / total
    congestion = CONGESTION_COEF * np.maximum(0.0, utilization - 0.85)
    inhouse_cost = (
        FULFILMENT_BASE
        * (1 + FULFILMENT_INFLATION) ** years
        * (1 + congestion)
        * np.exp(rng.normal(0, 0.015, n))
    )
    spot_fee = SPOT_3PL_BASE * 1.03**years * np.exp(rng.normal(0, 0.02, n))

    # Marketing is planned against the seasonal forecast, so it tracks orders closely.
    marketing = MARKETING_PER_ORDER * expected.sum(axis=(1, 2)) * np.exp(rng.normal(0, 0.07, n))

    # Customer base follows the deseasonalized trend, not monthly noise. Price effects reach
    # the base gradually (exponential smoothing), otherwise one broad price change would
    # imply negative customer acquisition in a single month.
    blended_effect = price_effect @ cat_share / cat_share.sum()
    smoothed_effect = np.empty(n)
    smoothed_effect[0] = blended_effect[0]
    for t in range(1, n):
        smoothed_effect[t] = 0.8 * smoothed_effect[t - 1] + 0.2 * blended_effect[t]
    trend_orders = region_trend.sum(axis=1) * cat_share.sum() * smoothed_effect * scale
    active = np.round(trend_orders / ORDERS_PER_ACTIVE_CUSTOMER * np.exp(rng.normal(0, 0.004, n))).astype(
        np.int64
    )
    churn = BASE_CHURN + rng.normal(0, 0.0015, n)
    order_mix = orders[-12:].sum(axis=(0, 1)) / orders[-12:].sum()
    for change in PRICE_CHANGES:
        t0 = month_index(change.month)
        breadth = order_mix[affected_categories(change)].sum()
        bump = 0.005 * breadth * change.pct / 0.05
        churn[t0] += bump
        if t0 + 1 < n:
            churn[t0 + 1] += bump / 2
    churn = np.round(churn, 4)
    previous = np.concatenate([[round(active[0] / 1.009)], active[:-1]])
    churned = np.round(churn * previous).astype(np.int64)
    new = active - previous + churned

    overtime = np.round(
        (120 + 4000 * np.maximum(0.0, utilization - 0.85)) * np.exp(rng.normal(0, 0.1, n))
    ).astype(np.int64)
    on_time = np.clip(
        0.985 - 0.6 * np.maximum(0.0, utilization - 0.88) - 0.15 * overflow_share + rng.normal(0, 0.003, n),
        0.85,
        0.995,
    )

    return CompanyHistory(
        seed=seed,
        months=tuple(month_label(t) for t in range(n)),
        orders=orders,
        expected_orders=expected,
        common_shock=common,
        price_index=np.round(price_index, 4),
        avg_price=avg_price,
        cogs_per_order=cogs_per_order,
        capacity=capacity.astype(np.int64),
        inhouse_cost_per_order=np.round(inhouse_cost, 2),
        spot_fee_per_order=np.round(spot_fee, 2),
        marketing_spend=np.round(marketing, 2),
        active_customers=active,
        churn_rate=churn,
        churned_customers=churned,
        new_customers=new,
        overtime_hours=overtime,
        on_time_ship_rate=np.round(on_time, 4),
    )
