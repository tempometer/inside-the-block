# Inside the Block — analysis table (auto-generated; all stats computed from the listed records)

source: data/mainnet · unique txs: 398 · attributed: 398 · leaders: 41 · skip counts: {'no_slot': 579, 'signature_missing': 0, 'block_unavailable': 0}

fee-rho / sendA-rho: per-slot Spearman inside single blocks (slot n≥5). sendA = arrival proxy from A-ONLY groups; mixed groups are descriptive only. registry_is_jito = validator-registry flag of the producer (our transport is public RPC; no relay-path comparison is claimed). k = slot-level correlations; neg counts negative ones; mean is their average; sign-p = exact one-sided sign-test p-value.

| family | leaders with ≥5 tx | txs | fee-ρ neg/k (mean, sign-p) | sendA-ρ k (mean) | mixed-ρ k (mean, descr.) |
|---|---|---|---|---|---|
| 26.10.4\|JITO | 3 | 71 | 3/4 (-0.3541, p=0.3125) | 1 (0.1579) | 4 (-0.239) |
| 4.3.2\|JITO | 17 | 268 | 8/11 (-0.4308, p=0.1133) | 8 (0.1626) | 11 (-0.2) |
| **POOLED** | — | — | **11/15 (-0.4104, p=0.0592)** | — | — |

Per-leader records (run, slot, n, probes, rho) live in analysis JSON; raw sigs/slots in data/.