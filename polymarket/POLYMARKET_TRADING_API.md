# Polymarket CLOB Trading API — How It Works

_Written 2026-06-21 based on official docs at docs.polymarket.com_

---

## 1. The Big Picture

Polymarket runs on Polygon (chain ID 137). Every market is a pair of ERC-1155 outcome tokens
(YES / UP and NO / DOWN) backed by pUSD collateral. When a market resolves, the winning token
pays $1 and the losing token pays $0.

Trading happens on a Central Limit Order Book (CLOB) operated by Polymarket off-chain, with
settlement on-chain via the Conditional Token Framework (CTF). Orders are signed locally with
your private key (EIP-712) and submitted to Polymarket's matching engine — no gas cost per order,
only on settlement.

---

## 2. Wallets & Accounts

There are three wallet types. **Deposit wallets are recommended for API users.**

| Type | Signature | Gas | Notes |
|------|-----------|-----|-------|
| EOA | Standard ETH | You pay POL | Simplest setup |
| Deposit Wallet | POLY_1271 (ERC-1271) | Gasless via relayer | **Recommended for bots** |
| Gnosis Safe | GNOSIS_SAFE | Gasless via relayer | For teams/multi-sig |

For a deposit wallet:
- You generate a regular private key (or use an existing one)
- Polymarket deploys a proxy wallet contract on your behalf (gasless via their relayer)
- Your private key signs orders on behalf of that proxy wallet
- Funds live in the proxy wallet address, not your key's EOA address

---

## 3. Authentication: Two Layers

### Layer 1 (L1) — Private Key, used once to generate L2 credentials

Signs an EIP-712 message proving you own the wallet. Used to call:
- `POST https://clob.polymarket.com/auth/api-key` → create new L2 credentials
- `GET  https://clob.polymarket.com/auth/derive-api-key` → re-derive if you lose them

L1 headers required:
```
POLY_ADDRESS      your wallet address (proxy wallet, not EOA)
POLY_SIGNATURE    EIP-712 signature of ClobAuthDomain message
POLY_TIMESTAMP    current UNIX timestamp (seconds)
POLY_NONCE        incrementing nonce
```

### Layer 2 (L2) — API Credentials, used on every request

Returned from L1 call:
```json
{
  "apiKey":     "550e8400-e29b-41d4-a716-446655440000",
  "secret":     "base64EncodedSecretString",
  "passphrase": "randomPassphraseString"
}
```

Every authenticated REST request requires these 5 headers:
```
POLY_ADDRESS     your wallet address
POLY_API_KEY     apiKey from above
POLY_SIGNATURE   HMAC-SHA256(secret, timestamp + method + path + body)
POLY_TIMESTAMP   current UNIX timestamp
POLY_PASSPHRASE  passphrase from above
```

**Orders still need an additional EIP-712 signature on the order itself** — L2 headers alone aren't
enough to place orders. The Python SDK handles all of this for you.

---

## 4. One-Time Setup (do this once per wallet)

After funding with pUSD, approve three token/contract pairs:

1. pUSD → CTF Contract (allows splitting pUSD into YES/NO outcome tokens)
2. CTF outcome tokens → CTF Exchange (allows the CLOB to move tokens on fills)
3. CTF outcome tokens → Neg Risk CTF Exchange (for markets that use neg-risk structure)

In the Python SDK: `await client.setup_trading_approvals()` handles all three.

---

## 5. Order Types

| Type | Behavior |
|------|----------|
| **GTC** (Good-Til-Cancelled) | Rests on the book until filled or you cancel — **use this for limit orders** |
| **GTD** (Good-Til-Date) | Auto-expires at a Unix timestamp you specify (min: now + 60s) |
| **FOK** (Fill-Or-Kill) | Must fill completely and immediately, else cancelled |
| **FAK** (Fill-And-Kill) | Fills whatever is available immediately, cancels the rest |

For market making: **GTC** is standard. Post a limit at your chosen price, it rests on the book.

---

## 6. Order Parameters

| Field | Type | Notes |
|-------|------|-------|
| `token_id` | string | The YES or NO token ID for the specific contract |
| `side` | "BUY" \| "SELL" | BUY = acquire tokens; SELL = sell tokens you hold |
| `price` | string | e.g. "0.52" — must match tick size |
| `size` | string | Number of shares (= USDC cost if buying at that price) |
| `order_type` | "GTC" \| "GTD" \| "FOK" \| "FAK" | Default: GTC |

**Tick sizes** — check per market; most crypto markets use 0.01. Orders priced off-tick are
rejected. Query: `GET https://clob.polymarket.com/tick-size?token_id=<id>`

**Max order size** is limited by your current balance minus existing open order reserves.

For market making, buying the YES token at 0.52 means:
- You pay 0.52 USDC per share
- If market resolves Up: you receive 1.00 USDC per share (+0.48 gross)
- If market resolves Down: you receive 0.00 USDC per share (−0.52 gross)

---

## 7. Fees

**Makers pay zero fees.** Only takers pay.

Taker fee formula: `fee = shares × feeRate × price × (1 − price)`

| Category | Taker Fee Rate |
|----------|---------------|
| Crypto (XRP, BTC, ETH…) | 0.07 |
| Finance / Politics | 0.04 |
| Sports | 0.03 |
| Geopolitical | 0 |

Example: 100 shares at $0.50 in a crypto market → taker fee = 100 × 0.07 × 0.50 × 0.50 = **$1.75**

As a **maker** (posting GTC limit orders), you pay **$0** in fees and may earn **daily USDC
rebates** from Polymarket's liquidity rewards program (scored by spread tightness and two-sided
depth).

---

## 8. Heartbeat Requirement

The CLOB requires a heartbeat every 10 seconds when you have open orders. If the heartbeat
stops for too long, **all your open orders are automatically cancelled.**

```
POST https://clob.polymarket.com/heartbeat
(with L2 auth headers)
```

This is critical for a trading bot — build heartbeat into the main loop.

---

## 9. WebSocket — Order & Fill Updates

Connect to the user channel to get real-time fill notifications:

```
wss://ws-subscriptions-clob.polymarket.com/ws/user
```

Authenticate on first message:
```json
{
  "auth": {
    "apiKey": "...",
    "secret": "...",
    "passphrase": "..."
  },
  "type": "user"
}
```

Optionally filter to specific markets:
```json
{
  "auth": { ... },
  "type": "user",
  "markets": ["0xabc...", "0xdef..."]
}
```

Message types received:
- **Order update** — placement, partial fill, cancellation (includes status: LIVE/MATCHED/CANCELED)
- **Trade event** — fill with size, price, trade ID, TAKER or MAKER perspective

Send `"PING"` every 10 seconds; server responds with `"PONG"`.

---

## 10. Key REST Endpoints

| Action | Method | Endpoint |
|--------|--------|----------|
| Post single order | POST | `https://clob.polymarket.com/order` |
| Post batch (≤15) | POST | `https://clob.polymarket.com/orders` |
| Cancel single | DELETE | `https://clob.polymarket.com/order` |
| Cancel all | DELETE | `https://clob.polymarket.com/cancel-all` |
| Cancel by market | DELETE | `https://clob.polymarket.com/cancel-market-orders` |
| Get open orders | GET | `https://clob.polymarket.com/data/orders?market=<cid>` |
| Get my trades | GET | `https://clob.polymarket.com/data/trades` |
| Get order book | GET | `https://clob.polymarket.com/book?token_id=<id>` |
| Get tick size | GET | `https://clob.polymarket.com/tick-size?token_id=<id>` |
| Heartbeat | POST | `https://clob.polymarket.com/heartbeat` |

Rate limits are generous: 5,000 order POSTs per 10s burst; 9,000 general per 10s.

---

## 11. Python SDK

Install: `pip install polymarket-client`

```python
import asyncio, os
from polymarket import AsyncSecureClient

async def main():
    async with await AsyncSecureClient.create(
        private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
        wallet=os.environ.get("POLYMARKET_WALLET_ADDRESS"),  # proxy wallet address
    ) as client:
        # one-time setup (approvals)
        await client.setup_trading_approvals()

        # place a GTC limit order to BUY YES token at 0.52
        resp = await client.place_limit_order(
            token_id="<yes_token_id>",
            side="BUY",
            price="0.52",
            size="10",
        )
        print(resp.order_id, resp.status)

        # cancel it
        await client.cancel_order(order_id=resp.order_id)

asyncio.run(main())
```

---

## 12. What We Need to Trade Live

To deploy the XRP market-making bot, we need:

| Item | Status | Notes |
|------|--------|-------|
| **Private key** | ❌ need from you | Signs orders (never leaves your machine) |
| **Proxy wallet address** | ❌ need from you | Where USDC lives on Polygon; OR we can derive from key |
| **pUSD balance on Polygon** | ❌ need from you | Starting capital (USDC bridged to Polygon) |
| **L2 API credentials** | auto-derived | Generated from private key on first run |
| **Token approvals** | auto-setup | `setup_trading_approvals()` handles this |

The bot will **never custody funds** — your private key stays in a local `.env` file, all signing
happens locally, and USDC lives in your Polygon wallet.
