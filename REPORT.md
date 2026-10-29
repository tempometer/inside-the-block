# Inside the Block — measurement report

Measurement dates: 2026-10-24 (devnet calibration) · 2026-10-26/27 night (mainnet
campaign, 312 txs RPC-confirmed included, all 312 chain-verified — one tx initially unrecoverable was re-found in its block during a subsequent re-fetch (slot 461832915, position 157) and merged back) · 2026-10-27 evening (mainnet completion: 80
battery txs + 6 conflict-pair winners of 12 sent). All positions and fees below are recomputed from
`getBlock` by `verify.py` (one command: see README "Reproduce"); every number in this
report traces to a raw signatures+slots log shipped in `data/`.

## Requirements coverage

| listing requirement | status | evidence |
|---|---|---|
| own MAINNET transactions | ✅ | 398 unique txs included in blocks, chain-verified |
| ≥6 validators | ✅ | 41 distinct block producers observed (20 with ≥5 txs each) |
| ≥3 distinct client/fork versions | ⚠️ partial | data spans **2 versions** (agave 4.3.2, Firedancer-derived 26.10.4); additional coverage attempts are documented in `data/README.md` |
| fee budget cap ~0.05 SOL | ✅/⚠️ | 50.97M lamports total on-chain fees + 5.35M stranded float (see Budget audit); funding was 0.059 SOL + 0.0096 top-up and the probe-fee SUBTOTAL alone exceeds the nominal 0.05 cap — full reconciliation is impossible from shipped data (budget audit below) |
| infer ordering rule (or absence) | ✅ | findings below, with explicit sample sizes |

Why only two versions: the 3rd/4th-version validators hold ≈0.4% of stake (≈0.4% of
slots), so once precision aiming failed and attribution became random landing, the
expected hits on them from ~150 probe slots was ≈0.6 (P(zero)≈0.55) — a quantitative
consequence of the aiming failure, not of target choice. Two dedicated version-coverage
trickle attempts followed the campaign: one misfired early due to a timer-cap bug
(515 base-fee txs landed as bonus A-probe data, none in-window); the second was fully
absorbed by per-IP relay quota after the first attempt's 596 sends (see `data/README.md`).
Both observed producers are flagged JITO in the validator registry; our measured
transport is public RPC and no relay-path comparison is claimed.

## Findings

### 1. Conflict pairs: first-sent always wins, loser always dropped — 6/6 mainnet, 6/6 devnet

Each pair: a throwaway source funded to exactly `transfer + base fee`, then two mutually
exclusive transfers (S→D1, S→D2, same amount) submitted sequentially in a fixed order
(d1 ~110–200 ms before d2). On mainnet, all 6 first-sent transfers landed (positions-among-successful
12–153 (absolute block positions 15–172) in 396–634-tx blocks, slots 462216051–57; producers: 2× agave-4.3.2, 1× FD-26.10.4);
all 6 second-sent transfers were dropped (no slot, never included).

Careful reading (limitation): after the winner lands, the source is drained to zero by the winner
(chain receipts: pre-balance 895,880 → post 0, fee 5,000 included), so a dropped loser
is the mechanically expected outcome of the fee-payer check — this experiment shows that NO observed client reorders the pair (e.g., by
including the loser as a failed transaction, or favoring it). It does NOT establish
WHERE the first-seen-wins decision happens (mempool vs block assembly); 3 producers is
too few for family claims, and the sequential (non-simultaneous) submission means this
is not a race test.

### 2. Fee ordering: priority fee correlates with earlier position in BOTH families

Per-slot Spearman ρ(fee tier, position-among-successful) inside single blocks
(slot-level n ≥ 5; never pooled across blocks/runs — see Methods):

| family (version\|registry) | leaders ≥5 txs | txs | fee-ρ neg/k (mean, sign-p) | sendA-ρ k (mean) | mixed-ρ k (mean, descr.) |
|---|---|---|---|---|---|
| agave 4.3.2 \| JITO | 17 | 268 | **8/11 (−0.4308, p=0.1133)** | 8 (+0.1626) | 11 (−0.2) |
| FD-derived 26.10.4 \| JITO | 3 | 71 | **3/4 (−0.3541, p=0.3125)** | 1 (+0.1579) | 4 (−0.239) |
| **POOLED** | — | — | **11/15 (−0.4104, p=0.0592)** | — | — |

Both families show a negative fee→position direction consistent with fee-priority
ordering (same numbers as the auto-generated `analysis-table.md`). Pooled 11/15
negative, mean ρ=−0.41, p=0.0592 — not statistically significant at alpha=0.05;
suggestive, not established. **We find NO statistically meaningful difference
between the families** (and note: repeated slots of the same leader are not
independent validators — this is an exploratory family-level summary). An earlier internal draft claimed a 1.8× stronger signal in the FD family;
that was an artifact of a double-counting bug (192/396 rows counted twice) and is
retracted — see What went wrong.

### 3. Arrival order: undetectable through the public-RPC path

For the A-ONLY arrival proxy (groups containing only identical-fee probes): agave
mean ρ(send order, position) = +0.1626 (k=8), FD +0.1579 (k=1) — a weak POSITIVE
tendency, unlike the mixed A/B groups (descriptive means −0.20 / −0.24; those groups
measure a different estimand and are reported separately). A devnet
end-to-end calibration measured ρ≈0.42 for a 20-tx salvo — an end-to-end baseline
that does NOT localise where order scrambles (RPC, relay, or leader intake); direct
TPU submission remains the follow-up that would isolate validator behavior.

### 4. Relay latency (measurement-infrastructure finding)

Public-RPC fire→inclusion latency varied 8–30 s and was bimodal across our salvos,
making slot-precise aiming at a leader window infeasible. All probes were therefore
attributed post-hoc by the actual block producer (epoch-1069 leader-schedule snapshot
shipped in `data/mainnet/`).

## Methods summary

Probes: A (identical-fee memo burst), B (fee ladder via SetComputeUnitPrice: 0–10M
µlamports/CU at night, 0–250k in the evening), C (conflict pairs as above), D (overload).
Confound disclosure: in the night runs the fee ladder was MONOTONIC in send order
(higher fees sent later within each bracket) — the planned anti-correlated design was
not implemented. The confound works AGAINST the fee finding: pure arrival-FIFO would
push the fee→position correlation POSITIVE (later-sent = higher fee = later position);
the observed negative correlation cannot be a FIFO artifact.

D never fired: public-relay rate limits make a 120-tx single-window burst infeasible
(documented limitation). A+B fired as composite salvos per target window (night:
bracket salvos of ~20 txs per wave, 62–70 verified per run; evening: 5×4-tx), mixed
sequential/concurrent submission across two public endpoints. Attribution: landing-slot → producer identity via the shipped
epoch-1069 schedule snapshot; client version and the registry_is_jito flag from the
validator census (`p0-leader-clients.json`; dual-sourced, 8/8 agree; the flag is a
registry property of the producer — our measured transport is public RPC).
Family-level summary: `analysis-table.md`; per-leader records: `analysis.json`.

## Budget audit (from meta.fee of every verified tx)

Funding: the campaign wallet was funded with 0.059 SOL (2026-10-26) + 0.0096 SOL top-up (2026-10-27).
On-chain fees across the 398 verified probe transactions (a SUBTOTAL of total spend): **50,978,975 lamports**
(recomputable: `verify.py` writes `fee_lamports` per row). Additionally ~5.35M lamports
of conflict-pair float were stranded on destination addresses whose keys were not
persisted (our bug; see What went wrong #8). The largest single-tx fee was 2,035,000 lamports: 17 transactions at the 10M-µlamports
price tier alone burned 34.6M lamports (~68% of the subtotal). Root cause: priority fees
are billed per CU of the transaction's compute-unit LIMIT, and our txs never set
SetComputeUnitLimit — every tier billed on the ~203,000-CU default-billed denominator.
(Fixed post-campaign: all tx shapes now carry SetComputeUnitLimit(20k) — 10k failed a
read-only simulation at 11,295 CU consumed.) Beyond this subtotal, total spend also
includes funding/sweep/calibration txs outside the probe rows and ~2.575M lamports of
runaway-trickle fees — full reconciliation is impossible from shipped data alone; the
wallet ended the campaign near-empty (swept back; a small residual remains).

## What went wrong (summary; full log in README.md)

1. hex/base64 wire mismatch on the first live devnet run.
2. Devnet faucets dead for agents ecosystem-wide; devnet funds were obtained manually
   through the web faucet.
3. Per-signature polling caused 429 storms — fixed by batching.
4. Campaign subprocesses signed with an unfunded devnet key (missing --key-file),
   initially misdiagnosed as a relay issue.
5. Public-relay latency 8–30 s (bimodal) defeated window aiming → post-hoc attribution.
6. Per-IP send quotas: ~300 txs/day through two endpoints, then ack-without-landing
   for hours (four night salvos and a 20-pair conflict run landed zero).
7. Priority fees billed on the default 200k-CU limit burned 68% of the budget in 17 txs.
8. Conflict-pair float stranded on destination keys we failed to persist.
9. Our own verify tool once double-counted 192/396 rows, producing a retracted
   "family difference" headline.

See README.md, "What went wrong", for the detailed account.
