#!/usr/bin/env python3
"""
Retrain ETH T=265 model on ALL available data using the CFES procedure.

Training procedure:
  - One train_pair call (seed=0) on the full dataset — no CV, no seed pooling.
  - Same BEST_CFG as the walk-forward uses at each step.
  - Saves huber_yes_eth_t265.txt + huber_no_eth_t265.txt to polymarket/.

This is the live-deployment equivalent of the walk-forward's final training step.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "2026-06-17-research"))
sys.path.insert(0, str(REPO / "2026-06-20-research"))

import consistency_framework as cf

OUT_YES = REPO / "polymarket" / "huber_yes_eth_t265.txt"
OUT_NO  = REPO / "polymarket" / "huber_no_eth_t265.txt"

print("Loading ETH T=265 data...")
df = cf.load_frame("ETH", 265)
print(f"  {len(df):,} rows  |  {df['date'].nunique()} calendar days  |  "
      f"{df['date'].min()} → {df['date'].max()}")

print("Training on full dataset (seed=0)...")
m_yes, m_no = cf.train_pair(df, seed=0)

m_yes.save_model(str(OUT_YES))
m_no.save_model(str(OUT_NO))
print(f"Saved: {OUT_YES}")
print(f"Saved: {OUT_NO}")
print("Done.")
