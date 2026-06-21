# Profit Backtest Results — BTC Polymarket 5m, HF Spot Signal

**Date:** 2026-06-21  
**Script:** `profit_backtest.py` (corrected NO-trade PnL)  
**Data:** 2,911 BTC contracts Jun 8–19, official outcomes, Kraken HF 693K trades  
**Trials registered:** 19  

---

## Bug fix note

An earlier run had the NO-trade gross payoff inverted: `down_ask - outcome` instead of
`(1 - down_ask) - outcome`. Corrected before results below.

---

## No-arb scan (Phase 3)

- Buy-both violations: **0 / 2,911** (0.000%)
- **Result: Polymarket is well-priced. No locked profit.**

---

## Walk-forward (non-lockbox 7 days, expanding window)

T=180s primary. Day-clustered wild-bootstrap CI (2000 reps).

| Config | Exec | Mean PnL | CI [2.5%, 97.5%] | n | Trade% | Fill% |
|--------|------|----------|-----------------|---|--------|-------|
| 2-feat C=0.1 | Taker | −0.033 | [−0.049, −0.016] | 56 | 7.5% | — |
| 2-feat C=0.1 | Maker | +0.338 | [+0.330, +0.347] | 24 | — | 42.9% |
| 2-feat C=0.5 | Taker | −0.022 | [−0.059, +0.016] | 154 | 20.6% | — |
| 2-feat C=0.5 | Maker | +0.008 | [−0.019, +0.034] | 144 | — | 93.5% |
| 2-feat C=1.0 | Taker | +0.009 | [−0.031, +0.049] | 197 | 26.4% | — |
| 2-feat C=1.0 | **Maker** | **+0.035** | **[+0.005, +0.065]** | **187** | — | **94.9%** |
| 3-feat C=1.0 | Taker | −0.010 | [−0.043, +0.022] | 482 | 64.5% | — |
| 3-feat C=1.0 | Maker | +0.063 | [+0.024, +0.101] | 410 | — | 85.1% |

Robustness (2-feat C=1.0): T=120 maker −0.042; T=240 maker +0.036.

**C=0.1 maker caveat:** +0.338 from 24 fills at 42.9% fill rate — too few trades for reliable
inference despite tight CI. The lockbox fully reverses it (see below).

---

## Lockbox (Final Gate — proper method)

Train: all 7 non-lockbox days (1,799). Test: all 5 lockbox days (1,104). Single frozen block.

| Config | Exec | Mean PnL | CI [2.5%, 97.5%] | n | Trade% | Fill% |
|--------|------|----------|-----------------|---|--------|-------|
| 2-feat C=0.1 | Taker | −0.085 | [−0.160, −0.009] | 120 | 10.9% | — |
| 2-feat C=0.1 | Maker | −0.061 | [−0.130, +0.007] | 116 | — | 96.7% |
| 2-feat C=1.0 | Taker | −0.028 | [−0.051, −0.006] | 536 | 48.6% | — |
| 2-feat C=1.0 | Maker | **+0.004** | **[−0.014, +0.023]** | 517 | — | 96.5% |
| 3-feat C=1.0 | Taker | −0.024 | [−0.071, +0.024] | 654 | 59.3% | — |
| 3-feat C=1.0 | Maker | +0.033 | [−0.017, +0.082] | 595 | — | 91.0% |

C=0.1 collapses: −0.085 taker, −0.061 maker. Walk-forward winner was overfit to 24 samples.
C=1.0 maker: +0.004 [−0.014, +0.023] — near zero, CI straddles zero.

---

## Verdict

| Question | Answer |
|---|---|
| Taker profitable? | **No.** Lockbox C=1.0 taker CI [−0.051, −0.006], entirely negative. |
| Maker profitable after realistic fills? | **No (borderline).** Walk-forward CI_lo barely > 0 (+0.005), lockbox CI [−0.014, +0.023] straddles zero. |
| No-arb? | **None.** 0 violations. |
| Deployable? | **No.** Lockbox sealed negative. |

The mean-reversion signal (logit_mid + hf_ret_60s, C=1.0) yields walk-forward maker edge of
+3.5¢/contract [+0.5¢, +6.5¢] that does **not** survive the lockbox (+0.4¢ [−1.4¢, +2.3¢]).
Taker is negative throughout. This matches the spec's expected outcome: signal is sub-spread,
12 days is insufficient to confirm deployment.

**Next step:** accumulate ≥20 OOS days (~mid-July) and re-run the same pre-registered
procedure with frozen config (2-feat, C=1.0, T=180) before any further tuning.
