#!/usr/bin/env python3
import argparse
import json
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cli_server as cli
import kalshi_btc15_server as btc


class FatalTradeError(RuntimeError):
    pass


POLYMARKET_MARKET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
KALSHI_MARKET_CACHE: dict[str, dict[str, Any]] = {}


def http_json(
    method: str,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    auth: bool = False,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "btc15m-cli-trader/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers.update(btc.auth_headers(method, path))
    req = Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc


def as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def cents(value: float) -> int:
    return max(1, min(99, int(round(value * 100))))


def dollars(value: float) -> str:
    return f"{value:.4f}"


def format_balance_value(value: Any) -> str:
    number = as_float(value)
    if number >= 100:
        number /= 100.0
    return cli.fmt_money(number)


def format_usdc_base_units(value: Any) -> str:
    return cli.fmt_money(as_float(value) / 1_000_000.0)


def kalshi_balance_summary() -> str:
    data = http_json("GET", btc.BASE_URL, "/portfolio/balance", auth=True)
    balance = data.get("balance") if isinstance(data, dict) else None
    if isinstance(balance, dict):
        cash = (
            balance.get("cash_balance_dollars")
            or balance.get("cash_balance")
            or balance.get("balance_dollars")
            or balance.get("balance")
        )
        available = (
            balance.get("available_balance_dollars")
            or balance.get("available_balance")
            or balance.get("cash_available_dollars")
            or balance.get("cash_available")
        )
    else:
        cash = data.get("balance") if isinstance(data, dict) else None
        available = data.get("available_balance") if isinstance(data, dict) else None
    if available not in (None, ""):
        return f"Kalshi balance {format_balance_value(cash)} available {format_balance_value(available)}"
    return f"Kalshi balance {format_balance_value(cash)}"


def polymarket_balance_summary() -> str:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = polymarket_client_v2()
    data = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    if not isinstance(data, dict):
        return f"Polymarket balance response {data}"
    balance = data.get("balance") or data.get("usdc_balance") or data.get("collateral")
    allowances = data.get("allowances")
    if isinstance(allowances, dict) and allowances:
        allowance = max(as_float(value) for value in allowances.values())
    else:
        allowance = data.get("allowance") or data.get("usdc_allowance")
    return (
        f"Polymarket USDC balance {format_usdc_base_units(balance)} "
        f"allowance {format_usdc_base_units(allowance)}"
    )


def print_startup_balances() -> None:
    try:
        print(kalshi_balance_summary(), flush=True)
    except Exception as exc:
        print(f"Kalshi balance ERROR {type(exc).__name__}: {exc}", flush=True)
    try:
        print(polymarket_balance_summary(), flush=True)
    except Exception as exc:
        print(f"Polymarket balance ERROR {type(exc).__name__}: {exc}", flush=True)


def fill_count(order: dict[str, Any]) -> float:
    return as_float(order.get("fill_count_fp") or order.get("fill_count") or order.get("filled_count"))


def filled_price(order: dict[str, Any], side: str) -> float:
    filled = fill_count(order)
    cost = as_float(
        order.get("taker_fill_cost_dollars")
        or order.get("maker_fill_cost_dollars")
        or order.get("cost_dollars")
    )
    if filled and cost:
        return cost / filled
    return as_float(order.get(f"{side}_price_dollars")) or as_float(order.get(f"{side}_price")) / 100.0


def level_price(level: Any) -> float | None:
    if not isinstance(level, (list, tuple)) or not level:
        return None
    return btc.normalize_price(level[0])


def level_quantity(level: Any) -> float:
    if not isinstance(level, (list, tuple)) or len(level) < 2:
        return 0.0
    return as_float(level[1])


def kalshi_liquidity_plan_for_buy(ticker: str, side: str, price: float) -> dict[str, Any]:
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    yes_levels, no_levels = btc.orderbook_levels(orderbook)
    opposite_levels = no_levels if side == "yes" else yes_levels
    min_opposite_bid = round(1.0 - price, 10)
    priced_levels = [
        (opposite_bid, level_quantity(level))
        for level in opposite_levels
        if (opposite_bid := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0], reverse=True)

    cumulative = 0.0
    levels = []
    for opposite_bid, quantity in priced_levels:
        executable = opposite_bid >= min_opposite_bid
        if executable:
            cumulative += quantity
        levels.append(
            {
                "opposite_bid": opposite_bid,
                "buy_price": round(1.0 - opposite_bid, 10),
                "quantity": quantity,
                "cumulative": cumulative if executable else None,
                "executable": executable,
            }
        )

    return {
        "side": side,
        "limit_price": price,
        "min_opposite_bid": min_opposite_bid,
        "liquidity": cumulative,
        "levels": levels,
    }


def kalshi_resting_volume_for_buy(ticker: str, side: str, price: float) -> float:
    return as_float(kalshi_liquidity_plan_for_buy(ticker, side, price).get("liquidity"))


def kalshi_post_order(
    ticker: str,
    side: str,
    price: float,
    contracts: int,
    client_order_id: str,
    action: str = "buy",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "client_order_id": client_order_id,
        "count": contracts,
        "time_in_force": "fill_or_kill",
    }
    payload[f"{side}_price"] = cents(price)
    response = http_json("POST", btc.BASE_URL, "/portfolio/orders", payload=payload, auth=True)
    order = response.get("order") or response
    order_id = order.get("order_id")
    if order_id:
        verified = http_json("GET", btc.BASE_URL, f"/portfolio/orders/{order_id}", auth=True)
        return verified.get("order") or verified
    return order


def kalshi_current_bid(ticker: str, side: str) -> float | None:
    orderbook = btc.kalshi_get(f"/markets/{ticker}/orderbook", {"depth": btc.ORDERBOOK_DEPTH})
    yes_levels, no_levels = btc.orderbook_levels(orderbook)
    best_yes_bid, _best_yes_qty = btc.best_level(yes_levels)
    best_no_bid, _best_no_qty = btc.best_level(no_levels)
    return best_yes_bid if side == "yes" else best_no_bid


def opposite_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def kalshi_current_ask(ticker: str, side: str) -> float | None:
    opposite_bid = kalshi_current_bid(ticker, opposite_side(side))
    if opposite_bid is None:
        return None
    return round(1.0 - opposite_bid, 10)


def kalshi_exit_position(ticker: str, side: str, contracts: int) -> dict[str, Any]:
    bid = kalshi_current_bid(ticker, side)
    if bid is None:
        sell_error = RuntimeError(f"No Kalshi {side.upper()} bid available to exit {ticker}")
    else:
        try:
            exit_order = kalshi_post_order(
                ticker,
                side,
                bid,
                contracts,
                f"btc15-exit-{uuid.uuid4().hex[:19]}",
                action="sell",
            )
            if fill_count(exit_order) > 0:
                exit_order["exit_method"] = "sell"
                return exit_order
            sell_error = RuntimeError(f"Kalshi sell exit had no fill: {exit_order}")
        except Exception as exc:
            sell_error = exc

    hedge_side = opposite_side(side)
    hedge_ask = kalshi_current_ask(ticker, hedge_side)
    if hedge_ask is None:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); no {hedge_side.upper()} ask available to hedge"
        )
    try:
        hedge_order = kalshi_post_order(
            ticker,
            hedge_side,
            hedge_ask,
            contracts,
            f"btc15-hedge-{uuid.uuid4().hex[:18]}",
            action="buy",
        )
    except Exception as hedge_exc:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); "
            f"opposite hedge {hedge_side.upper()} buy @ {cli.fmt_display_cents(hedge_ask)}c failed: "
            f"{type(hedge_exc).__name__}: {hedge_exc}"
        ) from hedge_exc
    if fill_count(hedge_order) <= 0:
        raise RuntimeError(
            f"Kalshi sell exit failed ({sell_error}); opposite hedge had no fill: {hedge_order}"
        )
    hedge_order["exit_method"] = f"buy_{hedge_side}"
    return hedge_order


def token_ids_by_contract(market: dict[str, Any]) -> dict[str, str]:
    token_ids = btc.parse_json_list(market.get("clobTokenIds"))
    outcomes = [str(outcome).lower() for outcome in btc.parse_json_list(market.get("outcomes"))]
    if len(token_ids) < 2:
        raise RuntimeError("Polymarket market has no CLOB token ids")
    up_index = outcomes.index("up") if "up" in outcomes else 0
    down_index = outcomes.index("down") if "down" in outcomes else 1
    return {"YES": str(token_ids[up_index]), "NO": str(token_ids[down_index])}


def polymarket_resting_volume_for_buy(
    market: dict[str, Any],
    contract: str,
    limit_price: float,
) -> float:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    _bid_levels, ask_levels = btc.polymarket_book_levels(orderbook)
    return sum(
        level_quantity(level)
        for level in ask_levels
        if (level_price(level) or 1.0) <= limit_price
    )


def polymarket_execution_plan(
    market: dict[str, Any],
    contract: str,
    contracts: int,
) -> tuple[float | None, float, dict[str, Any]]:
    token_id = token_ids_by_contract(market)[contract]
    orderbook = btc.clob_get("/book", {"token_id": token_id})
    _bid_levels, ask_levels = btc.polymarket_book_levels(orderbook)
    priced_levels = [
        (price, level_quantity(level))
        for level in ask_levels
        if (price := level_price(level)) is not None
    ]
    priced_levels.sort(key=lambda item: item[0])
    cumulative = 0.0
    cumulative_cost = 0.0
    levels = []
    selected_price: float | None = None
    for price, quantity in priced_levels:
        remaining = max(0.0, contracts - cumulative)
        used = min(quantity, remaining)
        cumulative += quantity
        cumulative_cost += used * price
        selected = selected_price is None and cumulative >= contracts
        if selected:
            selected_price = price
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": used,
                "cumulative": cumulative,
                "selected": selected,
            }
        )
        if selected:
            break
    vwap_price = cumulative_cost / contracts if selected_price is not None and contracts else None
    plan = {
        "contract": contract,
        "token_id": token_id,
        "liquidity": cumulative,
        "levels": levels,
        "vwap_price": vwap_price,
    }
    if selected_price is not None:
        return selected_price, cumulative, plan
    for price, quantity in priced_levels[len(levels):]:
        cumulative += quantity
        levels.append(
            {
                "price": price,
                "quantity": quantity,
                "used": 0.0,
                "cumulative": cumulative,
                "selected": False,
            }
        )
    plan["liquidity"] = cumulative
    return None, cumulative, plan


def polymarket_client_v2() -> Any:
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2
    except ImportError as exc:
        raise RuntimeError(
            "Polymarket trading requires py-clob-client-v2. Install with: "
            "python3 -m pip install py-clob-client-v2"
        ) from exc

    key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PK")
    if not key:
        raise RuntimeError("Missing POLYMARKET_PRIVATE_KEY in .env")
    host = os.getenv("POLYMARKET_CLOB_URL", btc.POLYMARKET_CLOB_URL)
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    kwargs: dict[str, Any] = {"host": host, "chain_id": chain_id, "key": key}
    signature_type = os.getenv("POLYMARKET_SIGNATURE_TYPE")
    funder = (
        os.getenv("POLYMARET_ADDRESS")
        or os.getenv("POLYMARKET_ADDRESS")
        or os.getenv("POLYMARKET_FUNDER")
        or os.getenv("POLYMARKET_PROXY_FUNDER")
    )
    if funder and not signature_type:
        signature_type = str(int(SignatureTypeV2.POLY_1271))
    if signature_type:
        kwargs["signature_type"] = int(signature_type)
    if funder:
        kwargs["funder"] = funder

    if os.getenv("CLOB_API_KEY") and os.getenv("CLOB_SECRET") and os.getenv("CLOB_PASS_PHRASE"):
        creds = ApiCreds(
            api_key=os.environ["CLOB_API_KEY"],
            api_secret=os.environ["CLOB_SECRET"],
            api_passphrase=os.environ["CLOB_PASS_PHRASE"],
        )
    else:
        auth_client = ClobClient(**kwargs)
        try:
            creds = auth_client.derive_api_key()
        except Exception:
            creds = auth_client.create_api_key()
    if not all(
        getattr(creds, field, None) for field in ("api_key", "api_secret", "api_passphrase")
    ):
        raise RuntimeError("Polymarket API credentials are incomplete")
    return ClobClient(**kwargs, creds=creds)


def polymarket_post_order(
    market: dict[str, Any],
    contract: str,
    price: float,
    contracts: int,
) -> dict[str, Any]:
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

    token_id = token_ids_by_contract(market)[contract]
    client = polymarket_client_v2()
    response = client.create_and_post_order(
        order_args=OrderArgs(
            token_id=token_id,
            price=price,
            side=Side.BUY,
            size=float(contracts),
        ),
        options=PartialCreateOrderOptions(tick_size="0.01"),
        order_type=OrderType.FOK,
    )
    if not isinstance(response, dict):
        return {"response": response}
    order_id = (
        response.get("id")
        or response.get("order_id")
        or response.get("orderID")
        or response.get("orderId")
    )
    if order_id:
        try:
            verified = client.get_order(str(order_id))
        except Exception as exc:
            response["verification_error"] = f"{type(exc).__name__}: {exc}"
        else:
            response["verified_order"] = verified
    return response


def polymarket_fill_summary(response: dict[str, Any], expected_price: float) -> tuple[bool, float]:
    verified = response.get("verified_order")
    if isinstance(verified, dict):
        filled_size = as_float(
            verified.get("filled_size")
            or verified.get("matched_size")
            or verified.get("size_matched")
            or verified.get("fill_count")
        )
        average_price = as_float(
            verified.get("average_price")
            or verified.get("avg_price")
            or verified.get("price")
        )
        status = str(verified.get("status") or verified.get("state") or "").lower()
        if filled_size > 0 or "filled" in status or "matched" in status:
            return True, average_price or expected_price

    executions = response.get("executions")
    if isinstance(executions, list) and executions:
        total_size = sum(as_float(item.get("quantity") or item.get("size")) for item in executions)
        total_cost = sum(
            as_float(item.get("price", {}).get("value") if isinstance(item.get("price"), dict) else item.get("price"))
            * as_float(item.get("quantity") or item.get("size"))
            for item in executions
        )
        if total_size:
            return True, total_cost / total_size

    status_text = " ".join(str(value).lower() for value in response.values())
    filled = any(word in status_text for word in ("filled", "matched", "success"))
    return filled, expected_price


def cached_active_kalshi_market() -> dict[str, Any] | None:
    cached = KALSHI_MARKET_CACHE.get(btc.SERIES_TICKER)
    if cached:
        close_ts = btc.parse_ts(cached.get("close_time") or cached.get("close_ts"))
        if not close_ts or close_ts > time.time() + 5:
            return cached
    market = btc.discover_active_market()
    if market:
        KALSHI_MARKET_CACHE[btc.SERIES_TICKER] = market
    return market


def fetch_market_state() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    kalshi_market = cached_active_kalshi_market()
    if not kalshi_market:
        raise RuntimeError(f"No open market found for {btc.SERIES_TICKER}")
    cache_key = (
        str(kalshi_market.get("ticker") or ""),
        str(kalshi_market.get("close_time") or kalshi_market.get("close_ts") or ""),
    )
    kalshi_orderbook = btc.kalshi_get(
        f"/markets/{kalshi_market['ticker']}/orderbook", {"depth": btc.ORDERBOOK_DEPTH}
    )
    kalshi_snapshot = btc.make_snapshot(kalshi_market, kalshi_orderbook)

    polymarket_market = POLYMARKET_MARKET_CACHE.get(cache_key)
    if polymarket_market is None:
        polymarket_market = btc.discover_polymarket_market(kalshi_market)
        if polymarket_market is not None:
            POLYMARKET_MARKET_CACHE[cache_key] = polymarket_market
    if not polymarket_market:
        raise RuntimeError("No matching open Polymarket market found")
    polymarket_orderbook = btc.polymarket_clob_orderbooks(polymarket_market)
    polymarket_snapshot = btc.make_polymarket_snapshot(polymarket_market, polymarket_orderbook)
    return kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot


def polymarket_execution_price(price: float) -> float:
    return min(0.99, round(price + 0.01, 2))


def execution_expected_profit(arbitrage: dict[str, Any]) -> float:
    total_cost = arbitrage["kalshi_price"] + polymarket_execution_price(
        arbitrage["polymarket_price"]
    )
    if total_cost <= 0 or total_cost >= 1:
        return 0.0
    return (1.0 / total_cost) - 1.0


def trade_preflight(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    arbitrage: dict[str, Any],
    contracts: int,
    min_adjusted_profit: float,
) -> dict[str, Any]:
    kalshi_side = arbitrage["kalshi_contract"].lower()
    polymarket_contract = arbitrage["polymarket_contract"]
    kalshi_price = arbitrage["kalshi_price"]
    fallback_poly_price = polymarket_execution_price(arbitrage["polymarket_price"])
    poly_price, poly_liquidity, poly_plan = polymarket_execution_plan(
        polymarket_market,
        polymarket_contract,
        contracts,
    )
    poly_order_price = poly_price or fallback_poly_price
    total_cost = kalshi_price + poly_order_price
    adjusted_profit = (1.0 / total_cost) - 1.0 if 0 < total_cost < 1 else 0.0
    kalshi_plan = kalshi_liquidity_plan_for_buy(
        str(kalshi_snapshot["ticker"]),
        kalshi_side,
        kalshi_price,
    )
    kalshi_liquidity = as_float(kalshi_plan.get("liquidity"))
    vwap_price = poly_plan.get("vwap_price")
    vwap_total_cost = kalshi_price + vwap_price if isinstance(vwap_price, float) else None
    vwap_profit = (
        (1.0 / vwap_total_cost) - 1.0
        if isinstance(vwap_total_cost, float) and 0 < vwap_total_cost < 1
        else None
    )

    decision = "PLACE"
    reason = "liquidity ok"
    if poly_price is None:
        decision = "SKIP"
        reason = f"Polymarket liquidity {poly_liquidity:g} < {contracts}"
    elif adjusted_profit <= min_adjusted_profit:
        decision = "SKIP"
        reason = (
            f"adjusted profit {cli.fmt_money(adjusted_profit)} "
            f"<= {cli.fmt_money(min_adjusted_profit)}"
        )
    elif kalshi_liquidity < contracts:
        decision = "SKIP"
        reason = f"Kalshi liquidity {kalshi_liquidity:g} < {contracts}"

    return {
        "decision": decision,
        "reason": reason,
        "kalshi_side": kalshi_side,
        "kalshi_price": kalshi_price,
        "kalshi_liquidity": kalshi_liquidity,
        "polymarket_contract": polymarket_contract,
        "polymarket_price": poly_order_price,
        "polymarket_liquidity": poly_liquidity,
        "polymarket_vwap_price": vwap_price,
        "polymarket_vwap_profit": vwap_profit,
        "adjusted_profit": adjusted_profit,
        "kalshi_plan": kalshi_plan,
        "polymarket_plan": poly_plan,
    }


def format_preflight(preflight: dict[str, Any]) -> str:
    return (
        f"CHECK {preflight['decision']} {preflight['reason']} | "
        f"K {preflight['kalshi_side'].upper()} @ {cli.fmt_display_cents(preflight['kalshi_price'])}c "
        f"liq {preflight['kalshi_liquidity']:g} | "
        f"P {preflight['polymarket_contract']} @ {cli.fmt_display_cents(preflight['polymarket_price'])}c "
        f"liq {preflight['polymarket_liquidity']:g} | "
        f"adj profit {cli.fmt_money(preflight['adjusted_profit'])}"
    )


def format_depth_level(price: float, quantity: float, cumulative: Any | None = None) -> str:
    text = f"{cli.fmt_display_cents(price)}c x {quantity:g}"
    if isinstance(cumulative, (int, float)):
        text += f" cum {cumulative:g}"
    return text


def format_orderbook_debug(preflight: dict[str, Any], contracts: int, max_levels: int) -> list[str]:
    kalshi_plan = preflight["kalshi_plan"]
    poly_plan = preflight["polymarket_plan"]
    kalshi_side = preflight["kalshi_side"].upper()
    poly_contract = preflight["polymarket_contract"]
    max_levels = max(1, max_levels)

    kalshi_levels = [
        level for level in kalshi_plan.get("levels", []) if level.get("executable")
    ][:max_levels]
    if kalshi_levels:
        kalshi_text = ", ".join(
            format_depth_level(
                as_float(level.get("buy_price")),
                as_float(level.get("quantity")),
                level.get("cumulative"),
            )
            for level in kalshi_levels
        )
    else:
        kalshi_text = "none"

    poly_levels = poly_plan.get("levels", [])[:max_levels]
    if poly_levels:
        poly_text = ", ".join(
            (
                format_depth_level(
                    as_float(level.get("price")),
                    as_float(level.get("quantity")),
                    level.get("cumulative"),
                )
                + (" selected" if level.get("selected") else "")
            )
            for level in poly_levels
        )
    else:
        poly_text = "none"

    vwap_price = preflight.get("polymarket_vwap_price")
    vwap_profit = preflight.get("polymarket_vwap_profit")
    vwap_text = (
        f"book VWAP {cli.fmt_display_cents(vwap_price)}c profit {cli.fmt_money(vwap_profit)}"
        if isinstance(vwap_price, float) and isinstance(vwap_profit, float)
        else "book VWAP --"
    )

    return [
        (
            f"BOOK K {kalshi_side} buy limit {cli.fmt_display_cents(preflight['kalshi_price'])}c "
            f"needs opposite bid >= {cli.fmt_display_cents(kalshi_plan['min_opposite_bid'])}c; "
            f"executable {kalshi_plan['liquidity']:g}; levels {kalshi_text}"
        ),
        (
            f"BOOK P {poly_contract} buy size {contracts:g}; "
            f"asks {poly_text}; selected limit "
            f"{cli.fmt_display_cents(preflight['polymarket_price'])}c; "
            f"available {preflight['polymarket_liquidity']:g}"
        ),
        (
            f"BOOK adjusted limit cost "
            f"{cli.fmt_display_cents(preflight['kalshi_price'])}c + "
            f"{cli.fmt_display_cents(preflight['polymarket_price'])}c = "
            f"{cli.fmt_display_cents(preflight['kalshi_price'] + preflight['polymarket_price'])}c; "
            f"limit profit {cli.fmt_money(preflight['adjusted_profit'])}; {vwap_text}"
        ),
    ]


def execute_arbitrage(
    kalshi_snapshot: dict[str, Any],
    polymarket_market: dict[str, Any],
    arbitrage: dict[str, Any],
    contracts: int,
    min_adjusted_profit: float,
    live: bool,
    preflight: dict[str, Any] | None = None,
) -> str:
    kalshi_side = arbitrage["kalshi_contract"].lower()
    polymarket_contract = arbitrage["polymarket_contract"]
    kalshi_price = arbitrage["kalshi_price"]
    polymarket_price = arbitrage["polymarket_price"]
    if preflight is None:
        preflight = trade_preflight(
            kalshi_snapshot,
            polymarket_market,
            arbitrage,
            contracts,
            min_adjusted_profit,
        )
    poly_order_price = preflight["polymarket_price"]
    if not live:
        if preflight["decision"] != "PLACE":
            return f"DRY RUN would skip: {preflight['reason']}"
        return (
            "DRY RUN would place "
            f"Kalshi {kalshi_side.upper()} {contracts} @ {cli.fmt_display_cents(kalshi_price)}c and "
            f"Polymarket {polymarket_contract} {contracts} @ {cli.fmt_display_cents(poly_order_price)}c "
            f"({preflight['reason']})"
        )

    if preflight["decision"] != "PLACE":
        return (
            f"SKIP {preflight['reason']}"
        )

    live_preflight = trade_preflight(
        kalshi_snapshot,
        polymarket_market,
        arbitrage,
        contracts,
        min_adjusted_profit,
    )
    if live_preflight["decision"] != "PLACE":
        return f"SKIP live recheck: {live_preflight['reason']}"
    poly_order_price = live_preflight["polymarket_price"]

    client_order_id = f"btc15-arb-{uuid.uuid4().hex[:20]}"
    kalshi_order = kalshi_post_order(
        str(kalshi_snapshot["ticker"]),
        kalshi_side,
        kalshi_price,
        contracts,
        client_order_id,
    )
    kalshi_filled = fill_count(kalshi_order)
    if kalshi_filled <= 0:
        return f"KALSHI NOT FILLED status={kalshi_order.get('status', '--')} id={kalshi_order.get('order_id', '--')}"

    kalshi_fill_price = filled_price(kalshi_order, kalshi_side)
    post_kalshi_poly_price, post_kalshi_poly_liquidity, _post_kalshi_poly_plan = (
        polymarket_execution_plan(
            polymarket_market,
            polymarket_contract,
            contracts,
        )
    )
    if post_kalshi_poly_price is None:
        try:
            exit_order = kalshi_exit_position(
                str(kalshi_snapshot["ticker"]),
                kalshi_side,
                int(kalshi_filled),
            )
            exit_filled = fill_count(exit_order)
            exit_price = filled_price(exit_order, kalshi_side)
            exit_text = (
                f"EXITED Kalshi {kalshi_side.upper()} {exit_filled:g} @ "
                f"{cli.fmt_display_cents(exit_price)}c"
            )
        except Exception as exit_exc:
            exit_text = f"KALSHI EXIT FAILED: {type(exit_exc).__name__}: {exit_exc}"
        raise FatalTradeError(
            "Polymarket hedge liquidity disappeared after Kalshi fill; "
            f"Polymarket liquidity {post_kalshi_poly_liquidity:g} < {contracts}; "
            f"Kalshi fill {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c; "
            f"{exit_text}"
        )
    post_kalshi_cost = kalshi_fill_price + post_kalshi_poly_price
    post_kalshi_profit = (
        (1.0 / post_kalshi_cost) - 1.0 if 0 < post_kalshi_cost < 1 else 0.0
    )
    if post_kalshi_profit <= min_adjusted_profit:
        try:
            exit_order = kalshi_exit_position(
                str(kalshi_snapshot["ticker"]),
                kalshi_side,
                int(kalshi_filled),
            )
            exit_filled = fill_count(exit_order)
            exit_price = filled_price(exit_order, kalshi_side)
            exit_text = (
                f"EXITED Kalshi {kalshi_side.upper()} {exit_filled:g} @ "
                f"{cli.fmt_display_cents(exit_price)}c"
            )
        except Exception as exit_exc:
            exit_text = f"KALSHI EXIT FAILED: {type(exit_exc).__name__}: {exit_exc}"
        raise FatalTradeError(
            "Polymarket hedge no longer meets adjusted profit after Kalshi fill; "
            f"Kalshi fill {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c; "
            f"Polymarket {polymarket_contract} now @ {cli.fmt_display_cents(post_kalshi_poly_price)}c; "
            f"post-fill profit {cli.fmt_money(post_kalshi_profit)} <= "
            f"{cli.fmt_money(min_adjusted_profit)}; {exit_text}"
        )
    poly_order_price = post_kalshi_poly_price
    try:
        poly_response = polymarket_post_order(
            polymarket_market,
            polymarket_contract,
            poly_order_price,
            contracts,
        )
    except Exception as exc:
        try:
            exit_order = kalshi_exit_position(
                str(kalshi_snapshot["ticker"]),
                kalshi_side,
                int(kalshi_filled),
            )
            exit_filled = fill_count(exit_order)
            exit_price = filled_price(exit_order, kalshi_side)
            exit_text = (
                f"EXITED Kalshi {kalshi_side.upper()} {exit_filled:g} @ "
                f"{cli.fmt_display_cents(exit_price)}c"
            )
        except Exception as exit_exc:
            exit_text = f"KALSHI EXIT FAILED: {type(exit_exc).__name__}: {exit_exc}"
        raise FatalTradeError(
            "Polymarket hedge failed after Kalshi fill; "
            f"Kalshi fill {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c; "
            f"{exit_text}; original error: {type(exc).__name__}: {exc}"
        ) from exc

    poly_filled, poly_fill_price = polymarket_fill_summary(poly_response, poly_order_price)
    if not poly_filled:
        try:
            exit_order = kalshi_exit_position(
                str(kalshi_snapshot["ticker"]),
                kalshi_side,
                int(kalshi_filled),
            )
            exit_filled = fill_count(exit_order)
            exit_price = filled_price(exit_order, kalshi_side)
            exit_text = (
                f"EXITED Kalshi {kalshi_side.upper()} {exit_filled:g} @ "
                f"{cli.fmt_display_cents(exit_price)}c"
            )
        except Exception as exit_exc:
            exit_text = f"KALSHI EXIT FAILED: {type(exit_exc).__name__}: {exit_exc}"
        raise FatalTradeError(
            "Polymarket hedge not verified after Kalshi fill; "
            f"Kalshi fill {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c; "
            f"{exit_text}; Polymarket response={poly_response}"
        )
    return (
        "TRADED "
        f"Kalshi {kalshi_side.upper()} filled {kalshi_filled:g} @ {cli.fmt_display_cents(kalshi_fill_price)}c; "
        f"Polymarket {polymarket_contract} filled @ {cli.fmt_display_cents(poly_fill_price)}c "
        f"(limit {cli.fmt_display_cents(poly_order_price)}c)"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Kalshi/Polymarket BTC 15m arbitrage and optionally execute live trades."
    )
    parser.add_argument("--interval", type=float, default=btc.POLL_SECONDS)
    parser.add_argument("--csv-dir", type=Path, default=btc.DATA_DIR)
    parser.add_argument("--flush-every", type=int, default=1)
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.10,
        help="Minimum raw displayed profit that triggers a preflight check.",
    )
    parser.add_argument(
        "--min-adjusted-profit",
        type=float,
        default=0.10,
        help="Minimum executable adjusted profit required before live or dry-run trade placement.",
    )
    parser.add_argument("--contracts", type=int, default=1, help="Matched contracts/shares per leg.")
    parser.add_argument("--max-trades", type=int, default=1, help="Maximum live arbitrage executions.")
    parser.add_argument("--live", action="store_true", help="Actually submit orders. Omit for dry-run.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument(
        "--print-arb-orderbook",
        action="store_true",
        help="On profitable raw arbitrage, print book depth used to compute liquidity and adjusted profit.",
    )
    parser.add_argument(
        "--book-depth-levels",
        type=int,
        default=6,
        help="Maximum executable/book levels to print with --print-arb-orderbook.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    interval = max(0.1, args.interval)
    flush_every = max(1, args.flush_every)
    contracts = max(1, args.contracts)
    max_trades = max(0, args.max_trades)
    min_adjusted_profit = max(0.0, args.min_adjusted_profit)
    pending_rows: dict[Path, list[dict[str, Any]]] = {}
    trades_done = 0

    mode = "LIVE TRADING" if args.live else "DRY RUN"
    print(
        f"{mode}; raw threshold > {cli.fmt_money(args.min_profit)}; "
        f"adjusted threshold > {cli.fmt_money(min_adjusted_profit)}; contracts={contracts}; "
        f"max_trades={max_trades}; polling every {interval:g}s",
        flush=True,
    )
    print_startup_balances()
    while True:
        started = time.monotonic()
        try:
            _kalshi_market, kalshi_snapshot, polymarket_market, polymarket_snapshot = fetch_market_state()
            arbitrage = cli.best_arbitrage(kalshi_snapshot, polymarket_snapshot)
            csv_path = cli.csv_path_for_contract(args.csv_dir, kalshi_snapshot)
            rows = pending_rows.setdefault(csv_path, [])
            rows.append(cli.csv_row(kalshi_snapshot, polymarket_snapshot, arbitrage))
            cli.print_snapshot(kalshi_snapshot, polymarket_snapshot, arbitrage)

            if arbitrage and arbitrage["expected_profit"] > args.min_profit and trades_done < max_trades:
                preflight = trade_preflight(
                    kalshi_snapshot,
                    polymarket_market,
                    arbitrage,
                    contracts,
                    min_adjusted_profit,
                )
                print(
                    f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | "
                    f"{format_preflight(preflight)}",
                    flush=True,
                )
                if args.print_arb_orderbook:
                    for line in format_orderbook_debug(
                        preflight,
                        contracts,
                        args.book_depth_levels,
                    ):
                        print(
                            f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | "
                            f"{line}",
                            flush=True,
                        )
                result = execute_arbitrage(
                    kalshi_snapshot,
                    polymarket_market,
                    arbitrage,
                    contracts,
                    min_adjusted_profit,
                    args.live,
                    preflight,
                )
                if args.live and not result.startswith("SKIP "):
                    trades_done += 1
                print(f"{cli.fmt_display_time(kalshi_snapshot.get('timestamp_utc')):<10} | {result}", flush=True)

            if len(rows) >= flush_every:
                cli.append_rows(csv_path, rows)
                rows.clear()
        except KeyboardInterrupt:
            if pending_rows:
                cli.flush_pending(pending_rows)
            raise
        except FatalTradeError as exc:
            print(f"{btc.iso_utc()} | FATAL {exc}", flush=True)
            if pending_rows:
                cli.flush_pending(pending_rows)
            break
        except Exception as exc:
            print(f"{btc.iso_utc()} | ERROR {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=2)

        if args.once:
            break
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    main()
