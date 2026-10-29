# Inside the Block — how do Solana validators order transactions?

Empirical measurement project for the Superteam Ukraine engagement
«Inside the Block: How Solana Validators Order Transactions» (submission deadline
2026-11-05). We submitted tagged probe transactions through public RPC into the
leader windows of validators running different client families (Agave / Firedancer-
derived) and recomputed, straight from the chain, where each transaction landed,
attributing every probe to its actual block producer.

**Everything in the report is recomputable from raw data by a single command** —
`verify.py` pulls `getBlock` for every recorded signature+slot and rebuilds the
position tables independently of our analysis.

## What we measure (executed protocol)

| probe | shape as EXECUTED | ordering question |
|---|---|---|
| A | 10–50 identical-fee memo txs per target salvo (night: 50, evening: 10) | does send order correlate with block position? (arrival-FIFO) |
| B | 10–50 txs with a fee ladder (CU price: 0–10M µlamports at night, 0–250k in the evening) | do higher fees land earlier? |
| C | 6 conflict pairs: source funded exactly transfer+fee, two mutually exclusive spends sent sequentially in fixed order | does first-sent always win? is the loser dropped or included-failed? |
| D | — never executed | (overload probe: a 120-tx burst needs relay throughput public RPC forbids — documented limitation) |

A+B fired together as composite salvos per target window — night as 5 bracket salvos of
20 txs at graded leads, evening as 5 salvos of 4 txs. Planned-vs-executed: the original design targeted 152-tx single salvos into slot ~2 of a
leader window; public-relay latency (8–30 s, bimodal, operator-log observation) and
per-IP send quotas forced the smaller bracket design, one window per target, and
post-hoc attribution by the actual block producer.

Vocabulary: a *salvo* is a batch of probe transactions fired together; a *bracket* is a
sequence of salvos sent at different lead times before a target leader window; a
*trickle* is a low-rate stream of transactions. tx = transaction; CU = compute unit;
FD-derived = Firedancer-derived; **probe X** is the composite A/B probe (the table lists
the A–D designs; X fires A and B together in one window).

## Design choices (and why)

- **Raw JSON-RPC + `solders`, no SDK**: the toolchain is a handful of small files; wire
  behavior is explicit and auditable (and we got burned once by an implicit wire
  assumption — see *What went wrong*).
- **Fire-and-forget salvos**: sequential ack-wait submits cost ~120 ms/tx and stretch
  a burst over ~6 slots — a leader window is 4 slots (1.6 s). We prebuild every tx,
  then submit all concurrently. Timing caveat: the Windows monotonic clock ticks at
  15.6 ms, so sub-tick "0–16 ms" spans mean the submission order equals the thread
  submit order and the true wire order within a tick is unmeasured.
- **Second public endpoint**: solana-rpc.publicnode.com rejects the default
  python-urllib User-Agent with 403; we send a browser-style User-Agent header —
  plain JSON-RPC, no scraping.
- **Window aiming by public RPC only**: `getLeaderSchedule` (+ `getVoteAccounts` for
  the vote→identity mapping) tells us each target's window seconds ahead. No private
  infrastructure, no MEV relay access — judges can reproduce the aiming itself.
- **Verifiability as a product**: raw JSONL logs (signature, slot, send timestamps,
  submit latency) + `verify.py` recompute + `analyze.py` verdicts. No preprocessing
  losses; every run keeps its keys file (gitignored) so conflict-source balances are
  auditable.
- **Spend cap**: hard ledger (`data/mainnet/campaign-state.json`) + per-run cap check; the campaign
  wallet is a throwaway keypair funded with 0.059 SOL + 0.0096 top-up — the
  on-chain probe-fee subtotal (50.97M lamports) alone exceeds the nominal 0.05 cap and
  full reconciliation is impossible from shipped data (see REPORT budget audit).

## Limitations (stated up front)

- **Public-RPC path scrambles arrival order**: on devnet, a 20-tx concurrent salvo
  shows Spearman(send order, block position) ≈ 0.42 — the RPC fan-out itself partially
  shuffles arrival. Client send order is therefore a *noisy proxy* for arrival order,
  not ground truth. Direct TPU/QUIC submission to the leader would remove this noise
  (stretch goal; documented, not attempted within budget).
- Version identification is dual-source (validator registry + gossip self-report via
  `getClusterNodes`) but both ultimately trust the node's own version string.
- The producer's Jito status comes from a single validator-registry source (Jito
  endpoints were unreachable from our network). The registry flag does not identify the
  transaction's measured transport path.
- Budget limits per-window repetition (1 window per target (executed)); ordering rules that vary
  between windows of the same validator will show up as instability, not noise.

## What went wrong (honest log)

1. **hex/base64 mismatch** (devnet, 26.10): first live run sent hex payloads with
   `encoding: base64` → RPC `-32602` deserialize error, 5/5 txs rejected. Dry-run
   cannot catch wire-format bugs — calibration on a live cluster did.
2. **Devnet faucets**: public `requestAirdrop` rate-limited/dry (429) ecosystem-wide;
   the web faucet is humans-only ("AI agents should not use this faucet"). We obtained
   devnet funds manually through the web faucet; scripts never scraped it.
3. **Rate-limit storms**: per-signature confirmation polling (20+ calls) tripped
   public-RPC 429s mid-run. Fixed by batching (`getSignatureStatuses` takes ~256 sigs
   per call) and `getMultipleAccounts` for balances; reads moved to a second free
   endpoint (its own quota) to reserve the primary for sends.
4. **Sequential submits don't fit a window** (~120 ms/tx): re-architected to
   prebuild + concurrent fire-and-forget (this is why the composite salvo exists).
5. **A missing `--key-file` flag mimicked a relay problem** (mainnet, 26-27.10):
   campaign subprocesses silently signed with the unfunded DEVNET keypair; every salvo
   was acked by the RPC but never landed (cannot pay fees). Initially misdiagnosed as
   a relay issue (per-account shadow filters, quota theories) — the
   decisive control was a single funded-key tx landing in 5s. Lesson: verify the
   SIGNER, not just the send path.
6. **Public-relay forwarding latency is 8-30s and bimodal** (27.10): slot-precise
   aiming at a leader window through public RPC relays is INFEASIBLE — a bracket of
   salvos at graded leads still scattered across ±30s. Consequence: all probes are
   attributed POST-HOC by the landing slot's leader (from the epoch leader schedule),
   not by the intended window. Direct TPU/QUIC submission would remove this limitation.
7. **Priority-fee math bug in the spend cap** (27.10): µlamports-per-CU ladder summed
   as raw lamports → a 0.0006-SOL bracket was refused as "94.9M lamports > cap".
   Fixed with a CU-based estimate.
8. **Conflict-pair float stranded** (27.10): probe C persisted the SOURCE keys but not
   the DESTINATION keys; winners park the transfer on d1/d2 addresses — ~5.35M lamports
   unrecoverable. Fixed: every throwaway account that can receive a balance gets its
   key persisted before funding.
9. **Per-IP send quota exhaustion** (27.10 night): after ~300 txs through two public
   endpoints, every further send was acked-but-dropped for hours — 4 target salvos and
   a 20-pair conflict run landed 0 txs (kept in data/ as empty runs for the record);
   recovered next evening with 75 s inter-target cooldowns and 4-tx salvos.
10. **Version-coverage trickle, both attempts documented** (28.10): attempt 1 ran away
   (a 20-min cap in the slot-wait loop released the drip early; a lagging mirror kept it
   running) — 596 sent/515 landed base-fee memos, 0 in the target window, ~2.57M
   lamports; attempt 2 (fixed code) sent 58, all ack-but-dropped by the exhausted
   per-IP quota. Third-version coverage remains an honest ⚠️ in the scorecard.
11. **Double-counted statistics, retracted** (28.10, caught in independent review): our
   verify tool wrote one merged artifact for multi-file runs; the analysis then counted
   192/396 rows twice and produced an (incorrect) "FD family fee-signal 1.8× stronger"
   headline. Fixed: per-file artifacts + signature dedup; the honest result (similar
   fee-signal in both families) is in REPORT.md.

## RPC endpoints & history availability

Default RPC is api.mainnet-beta.solana.com; `--rpc` points verify anywhere else. There
is NO automatic fallback from the default endpoint — if a block 404s there, retry
manually with `--rpc https://solana-rpc.publicnode.com` (a second free endpoint whose
own failed getBlock calls fall back to the primary in code) or an archive node.
publicnode ALREADY PRUNED one of our older blocks during verification on 2026-10-28 (first
available block drifts); the primary still served all audited slots as of 2026-10-28.
History availability on public endpoints carries no guarantee until judging day.

## Cost safety (which commands can spend money)

ZERO-COST (no key, no spend — but dry runs DO need public-RPC READS: blockhash +
vote accounts; they are not offline):
- `python itb_sender.py probe --probe X` — dry build (no `--live` flag = nothing is signed for submission; uses an ephemeral in-memory key)
- `python verify.py --net mainnet data/mainnet/*.jsonl` — READS blocks via public RPC, recomputes positions
- `python analyze.py --net mainnet` — reads local artifacts + the leader schedule via public RPC
- `docker compose up` — runs the DRY walk only

COSTS SOL (only with an explicit `--live` flag AND a funded key in ./keys):
- `python itb_campaign.py --live` and `python itb_sender.py ... --live` — real transactions, hard spend cap enforced (see data/mainnet/campaign-state.json)

## Reproduce — judge path (free, fresh data, one gated end-to-end chain)

Everything below is public-RPC READS only: no key, no spend. Fresh results land on the
HOST in `./verify-out/` (mounted in docker-compose.yml) and persist across compose runs.

```bash
# 1. DRY walk: builds the full probe plan, sends nothing
docker compose up --build
# 2. full campaign re-verify + analysis, GATED: analyze runs only if verify exits 0
docker compose run --build --rm itb sh -c 'python verify.py --net mainnet --out-dir verify-out/campaign "data/mainnet/run-20261026-*.jsonl" "data/mainnet/run-20261027-*.jsonl" && python analyze.py --net mainnet --artifacts verify-out/campaign --out verify-out/campaign/analysis-table.md --json-out verify-out/campaign/analysis.json'
```

Expected end state: all 398 slotted campaign rows chain-verified, 41 leaders, pooled
11/15 negative at p=0.0592 in `verify-out/campaign/analysis-table.md`. The `&&` is the
workflow gate: verify.py exits 0 only when every input row is verified or an expected
never-landed (no-slot) row; exit 2 = INCOMPLETE (RPC gaps — block_unavailable /
signature_missing, printed per file); exit 1 = nothing verified. In both failure cases
the analysis step does NOT run — a partial replay can never pass as a complete one
(analyze itself intentionally accepts partial artifacts and exits 0, which is why the
chain, not the analyzer, is the gate). For a quick single-run sample, chain the same
way with `data/mainnet/run-20261027-230435-probeC.jsonl` and a different `--out-dir`.
The 2026-10-28 trickle logs are bonus A-probe data OUTSIDE the 398-row corpus —
verify them the same way but into a separate out-dir. The same two commands run on
the host without Docker (`python verify.py ...` → `python analyze.py --net mainnet
--artifacts verify-out/campaign ...`).

Operator only — NOT part of judging (spends SOL, explicit `--live`, hard cap):
`docker compose run itb python itb_campaign.py --live`.

Narrative report: `REPORT.md`. Generated summaries: `analysis-table.md` and
`analysis.json`, produced by `analyze.py` from chain-verified records.
Raw data: `data/mainnet/*.jsonl` (send logs), `*.verify.json` (chain recompute).

## Targets (planned) vs hit

Planned matrix: 8 leaders across four client-version groups and two registry
categories (Figment, Helius,
Jupiter, Everstake, two anonymous agave|TPU validators, Pebble FD|TPU, one
agave-4.5-alpha|TPU). Actual post-hoc attribution: 41 block producers received our
probes (all flagged JITO in the validator registry, which carries ~96% of stake — our
measured transport is public RPC and we make no relay-path comparison), including 3 of
the 8 planned targets (Jupiter 20 txs, Helius 12, Figment 2). The two dedicated
version-coverage trickle attempts at a 3rd client version both failed (timer bug;
then relay quota) and are documented in data/README.md — coverage stays at 2 versions.

Figment, Helius (agave|JITO) · Jupiter, Everstake (FD-derived|JITO) · two anonymous
agave|TPU validators · Pebble (FD-derived|TPU, rarest combo) · one agave-4.5-alpha|TPU.
Client versions cross-checked by two independent sources (8/8 agree); see
`p0-version-crosscheck.json`.
