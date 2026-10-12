from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .decimal_utils import D
from .models import MarketRules, Mode


DEFAULT_BTCUSDT_RULES = MarketRules(
    symbol="BTCUSDT",
    hb_trading_pair="BTC-USDT",
    base_asset="BTC",
    quote_asset="USDT",
    margin_asset="USDT",
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("50"),
    maker_fee_rate=Decimal("0.0002"),
    taker_fee_rate=Decimal("0.0005"),
    trigger_protect=Decimal("0.0500"),
)

DEFAULT_BTCUSDC_RULES = MarketRules(
    symbol="BTCUSDC",
    hb_trading_pair="BTC-USDC",
    base_asset="BTC",
    quote_asset="USDC",
    margin_asset="USDC",
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("50"),
    maker_fee_rate=Decimal("0.0002"),
    taker_fee_rate=Decimal("0.0005"),
    trigger_protect=Decimal("0.0500"),
)


@dataclass(slots=True)
class GridConfig:
    lower_price: Decimal
    upper_price: Decimal
    grid_count: int
    allocated_margin: Decimal
    order_quote: Decimal
    max_open_orders: int
    leverage: int
    stop_loss_price: Decimal
    daily_loss_limit: Decimal
    total_loss_limit: Decimal
    max_position_notional: Decimal
    max_open_order_notional: Decimal
    spread_bps: Decimal = Decimal("2")
    expected_funding_bps: Decimal = Decimal("3")
    stale_price_seconds: int = 15
    grid_spacing: str = "arithmetic"
    slippage_bps: Decimal = Decimal("5")


@dataclass(slots=True)
class RuntimeConfig:
    mode: Mode
    exchange: str
    symbol: str
    hb_connector: str
    hb_trading_pair: str
    market_rules: MarketRules
    grid: GridConfig
    database_path: Path
    host: str
    port: int
    live_trading_enabled: bool = False
    binance_base_url: str = "https://fapi.binance.com"
    api_key_env: str = "BINANCE_FUTURES_API_KEY"
    api_secret_env: str = "BINANCE_FUTURES_API_SECRET"


class ConfigError(ValueError):
    pass


def _read_decimal(data: dict[str, Any], key: str) -> Decimal:
    if key not in data:
        raise ConfigError(f"missing grid.{key}")
    return D(data[key])


def load_config(path: str | Path) -> RuntimeConfig:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    mode = Mode(raw.get("mode", "paper"))
    exchange = raw.get("exchange", "binance_perpetual")
    symbol = raw.get("symbol", "BTCUSDT")
    if exchange != "binance_perpetual":
        raise ConfigError("only binance_perpetual is supported")
    if symbol not in {"BTCUSDT", "BTCUSDC"}:
        raise ConfigError("only BTCUSDT or BTCUSDC may be configured")

    rules_raw = raw.get("market_rules") or {}
    rules = DEFAULT_BTCUSDC_RULES if symbol == "BTCUSDC" else DEFAULT_BTCUSDT_RULES
    rules = MarketRules(
        symbol=symbol,
        hb_trading_pair=rules_raw.get("hb_trading_pair", rules.hb_trading_pair),
        base_asset=rules_raw.get("base_asset", rules.base_asset),
        quote_asset=rules_raw.get("quote_asset", rules.quote_asset),
        margin_asset=rules_raw.get("margin_asset", rules.margin_asset),
        tick_size=D(rules_raw.get("tick_size", rules.tick_size)),
        step_size=D(rules_raw.get("step_size", rules.step_size)),
        min_qty=D(rules_raw.get("min_qty", rules.min_qty)),
        min_notional=D(rules_raw.get("min_notional", rules.min_notional)),
        maker_fee_rate=D(rules_raw.get("maker_fee_rate", rules.maker_fee_rate)),
        taker_fee_rate=D(rules_raw.get("taker_fee_rate", rules.taker_fee_rate)),
        trigger_protect=D(rules_raw.get("trigger_protect", rules.trigger_protect)),
    )

    grid_raw = raw.get("grid", {})
    grid = GridConfig(
        lower_price=_read_decimal(grid_raw, "lower_price"),
        upper_price=_read_decimal(grid_raw, "upper_price"),
        grid_count=strict_int(grid_raw.get("grid_count", 8), "grid_count"),
        allocated_margin=_read_decimal(grid_raw, "allocated_margin"),
        order_quote=_read_decimal(grid_raw, "order_quote"),
        max_open_orders=strict_int(grid_raw.get("max_open_orders", 3), "max_open_orders"),
        leverage=strict_int(grid_raw.get("leverage", 1), "leverage"),
        stop_loss_price=_read_decimal(grid_raw, "stop_loss_price"),
        daily_loss_limit=_read_decimal(grid_raw, "daily_loss_limit"),
        total_loss_limit=_read_decimal(grid_raw, "total_loss_limit"),
        max_position_notional=_read_decimal(grid_raw, "max_position_notional"),
        max_open_order_notional=_read_decimal(grid_raw, "max_open_order_notional"),
        spread_bps=D(grid_raw.get("spread_bps", 2)),
        expected_funding_bps=D(grid_raw.get("expected_funding_bps", 3)),
        stale_price_seconds=strict_int(grid_raw.get("stale_price_seconds", 15), "stale_price_seconds"),
        grid_spacing=grid_raw.get("grid_spacing", "arithmetic"),
        slippage_bps=D(grid_raw.get("slippage_bps", "5")),
    )
    validate_grid_config(grid, rules)

    if mode is Mode.LIVE and not raw.get("live_trading_enabled", False):
        raise ConfigError("live mode requires live_trading_enabled=true")
    if raw.get("host", "127.0.0.1") not in {"127.0.0.1", "localhost"}:
        raise ConfigError("dashboard host must be loopback")
    if mode is Mode.TESTNET and raw.get("binance_base_url") not in {
        "https://testnet.binancefuture.com", "https://demo-fapi.binance.com"
    }:
        raise ConfigError("testnet requires an explicit Binance testnet endpoint")

    return RuntimeConfig(
        mode=mode,
        exchange=exchange,
        symbol=symbol,
        hb_connector="binance_perpetual",
        hb_trading_pair=rules.hb_trading_pair,
        market_rules=rules,
        grid=grid,
        database_path=Path(raw.get("database_path", "data/kgridbot.sqlite3")),
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8765)),
        live_trading_enabled=bool(raw.get("live_trading_enabled", False)),
        binance_base_url=raw.get("binance_base_url", "https://fapi.binance.com"),
        api_key_env=raw.get("api_key_env", "BINANCE_FUTURES_API_KEY"),
        api_secret_env=raw.get("api_secret_env", "BINANCE_FUTURES_API_SECRET"),
    )


def validate_grid_config(grid: GridConfig, rules: MarketRules) -> None:
    for field in fields(grid):
        value = getattr(grid, field.name)
        if isinstance(value, Decimal) and not value.is_finite():
            raise ConfigError(f"{field.name} must be finite")
    for name in ("tick_size", "step_size", "min_qty", "min_notional"):
        value = getattr(rules, name)
        if not value.is_finite() or value <= 0:
            raise ConfigError(f"{name} must be finite and positive")
    for name in ("maker_fee_rate", "taker_fee_rate"):
        value = getattr(rules, name)
        if not value.is_finite() or not 0 <= value < 1:
            raise ConfigError(f"{name} must be between 0 and 1")
    if not 1 <= grid.leverage <= 5:
        raise ConfigError("leverage must be between 1 and 5")
    if grid.lower_price <= 0 or grid.upper_price <= 0:
        raise ConfigError("grid prices must be positive")
    if grid.lower_price >= grid.upper_price:
        raise ConfigError("lower_price must be below upper_price")
    if not 2 <= grid.grid_count <= 200:
        raise ConfigError("grid_count must be between 2 and 200")
    if grid.order_quote < rules.min_notional:
        raise ConfigError("order_quote is below exchange min_notional")
    if not 1 <= grid.max_open_orders < grid.grid_count:
        raise ConfigError("max_open_orders must be between 1 and grid_count - 1")
    if grid.allocated_margin <= 0:
        raise ConfigError("allocated_margin must be positive")
    if grid.max_position_notional <= 0 or grid.max_open_order_notional <= 0:
        raise ConfigError("notional limits must be positive")
    if not 0 < grid.stop_loss_price < grid.lower_price:
        raise ConfigError("stop_loss_price must be below lower_price for a long grid")
    if grid.daily_loss_limit <= 0 or grid.total_loss_limit <= 0:
        raise ConfigError("loss limits must be positive")
    if grid.daily_loss_limit > grid.total_loss_limit:
        raise ConfigError("daily loss limit must not exceed total loss limit")
    if not 1 <= grid.stale_price_seconds <= 300:
        raise ConfigError("stale_price_seconds must be between 1 and 300")
    if min(grid.spread_bps, grid.expected_funding_bps, grid.slippage_bps) < 0:
        raise ConfigError("estimated costs must be nonnegative")
    if grid.grid_spacing not in {"arithmetic", "geometric"}:
        raise ConfigError("grid_spacing must be arithmetic or geometric")
    from .strategy import GridPlanner
    planner = GridPlanner(grid, rules)
    if len(set(planner.levels)) != grid.grid_count:
        raise ConfigError("grid levels collapse at the exchange tick size")
    friction = rules.maker_fee_rate * 2 + (grid.spread_bps + grid.expected_funding_bps + grid.slippage_bps) / 10000
    for index, price in enumerate(planner.levels[:-1]):
        if (planner.levels[index + 1] - price) / price <= friction:
            raise ConfigError("grid interval does not cover estimated fees, spread, slippage and funding")
        try:
            planner.make_entry_request(index, price, "validation")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc


def strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        number = D(value)
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError()
        return int(number)
    except (ValueError, ArithmeticError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def grid_json(grid: GridConfig) -> dict[str, Any]:
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(grid).items()}


def updated_grid(current: GridConfig, values: dict[str, Any], rules: MarketRules) -> GridConfig:
    if not isinstance(values, dict) or set(values) - set(grid_json(current)):
        raise ConfigError("unknown grid settings")
    converted = {}
    for key, value in values.items():
        old = getattr(current, key)
        try:
            converted[key] = D(value) if isinstance(old, Decimal) else strict_int(value, key) if isinstance(old, int) else value
        except ArithmeticError as exc:
            raise ConfigError(f"invalid {key}") from exc
    grid = replace(current, **converted)
    validate_grid_config(grid, rules)
    return grid
