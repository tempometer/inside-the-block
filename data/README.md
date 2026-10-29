# data/ — what every file is

All `run-*.jsonl` are raw sender logs: one JSON row per transaction with signature,
slot (once confirmed), send timestamps (monotonic + wall), fee tier, and submit latency.
`*.verify.json` are chain-recomputed artifacts from `verify.py`: per-row block position,
position-among-successful, on-chain error, and the actual on-chain fee (`fee_lamports`).

## mainnet/

- `run-20261026-235444-probeX.*` — calibration attempt, 5 txs, none landed (epoch/aim
  experiments); kept for the record.
- `run-20261027-000257 / 000719-probeX.*` — Figment-window salvos, 20+20 landed.
- `run-20261027-001450-probeC.*` — 20-pair conflict attempt: 0 landed (per-IP quota
  exhaustion after the night's ~300 txs); 23 of 40 sends were 429-rejected. Kept as the
  record of that failure mode.
- `run-20261027-002602 / 003219 / 004959 / 005214-probeX.*` — night bracket salvos
  (62–70 landed each — 70/70/70/62; the high-fee tiers here drove most of the campaign spend).
- `run-20261027-005519 / 005711 / 010214 / 010405-probeX.*` — ZERO-landing salvos:
  280 acked sends, 0 included (quota exhaustion wave). Kept as evidence; no fees paid
  for dropped txs.
- `run-20261027-222428 / 222824 / 224116 / 225400-probeX.*` — evening completion
  salvos (20/20 landed each, low-tier ladder).
- `run-20261027-230435-probeC.*` — the 6-pair mainnet conflict run (6/6 first-sent
  winners; losers dropped).
- `leader-schedule-epoch-1069.json` — snapshot of the epoch-1069 leader schedule used
  for post-hoc attribution (the live RPC only serves the CURRENT epoch).
- `campaign-state.json` — campaign budget ledger (caps, per-target run marks).

Key files (`*-keys.json`) are NOT shipped here: throwaway keypairs live only in the
operator's gitignored `keys/` directory.

## Reading and verifying the logs

- Run-tag timestamps in filenames are Europe/Kyiv local time; `send_wall` in rows is the
  BUILD time (UTC ISO), not the send time — send ordering lives in `send_mono_ns`.
- `run-20261027-002602` has 70 RPC-confirmed txs and 70 chain-verified: one landed
  signature was initially not locatable in its recorded block and the row was skipped.
  It was recovered by re-fetching the block (slot 461832915, position 157) and merged
  back — the shipped artifact contains all 70.
- `verify.py` re-run writes new artifacts into `verify-out/` — the shipped
  `*.verify.json` are never overwritten.
- `run-20261028-013155-trickle.jsonl` — version-coverage trickle attempt 1 (RUNAWAY):
  a 20-min cap in the slot-wait loop silently released the drip 14 min early and a
  lagging mirror kept it from stopping; 596 acked / 515 landed base-fee memos, 0 inside
  the target window. Cost ~2.57M lamports (515×5000). Kept: usable as post-hoc A-probe
  data across many leaders. Both bugs fixed (unbounded wait + wall-clock stop).
- `run-20261028-020313-trickle.jsonl` — version-coverage trickle attempt 2 (code fixed,
  correct window): 58 sent, 0 landed — per-IP relay quota exhausted after attempt 1's
  596 sends. No fees paid for dropped txs.

Notes: devnet calibration files are dated 24.10 and 26.10 (two calibration
sessions); `leader-schedule-epoch-1070.json` snapshots the epoch of the trickle
attempts; `run-20261027-232153-probeX.jsonl` is a dry-run log produced during packaging
validation. It records no on-chain activity and is not measurement evidence.
