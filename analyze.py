#!/usr/bin/env python3
"""ITB analyze — ordering signals per validator from chain-verified run data.

Analysis conventions:
- Stats are AUTO-GENERATED from one dataset: per-slot rho records carry (run, slot, n,
  probe-set, rho); family aggregates report k, negative-count, mean and an exact
  one-sided sign-test p — nothing hand-counted (an earlier hand-count overstated both
  the negative count and its significance before this automation; the retraction is
  documented in REPORT.md/README.md).
- Arrival proxy uses A-ONLY rows (mixed A/B groups measure a different estimand);
  mixed groups are reported separately as descriptive. The devnet 0.42 figure is an
  end-to-end baseline, NOT a localised "RPC ceiling".
- is_jito is a REGISTRY flag of the block producer (registry_is_jito) — our measured
  transport is public RPC, and no relay-path comparison is claimed.
- Reads verify artifacts from data/<net>/ by default or --artifacts <dir> (e.g.
  verify-out/ fresh judge output); dedups by signature; writes UTF-8 atomically.

Usage: python analyze.py --net mainnet [--artifacts verify-out] [--out analysis-table.md]
"""
import argparse
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# Use UTF-8 for console output so symbols render correctly on Windows
# (plain `python analyze.py` must work for a judge without extra flags).
for _s in (sys.stdout, sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "sender"))
from verify import spearman  # noqa: E402
from itb_sender import rpc_read  # noqa: E402

ENC = {"encoding": "utf-8"}


def datadir_for(net):
    d = HERE / "data" / net
    return d if d.exists() else HERE / "sender" / "data" / net


def load_schedule_snapshots(net):
    snaps = {}
    for f in sorted(datadir_for(net).glob("leader-schedule-epoch-*.json")):
        d = json.loads(f.read_text(**ENC))
        lo, n = d["first_absolute_slot"], d["slots_in_epoch"]
        slot2id = {}
        for ident, offs in d["leader_schedule"].items():
            for off in offs:
                slot2id[lo + off] = ident
        snaps[(lo, lo + n)] = slot2id
    return snaps


def load_census():
    d = json.loads((HERE / "p0-leader-clients.json").read_text(**ENC))
    return {r["identity"]: (str(r.get("version", "?")),
                            None if r.get("is_jito") is None else bool(r["is_jito"]))
            for r in d}


def load_target_names():
    xc = json.loads((HERE / "p0-version-crosscheck.json").read_text(**ENC))
    return {r["pubkey"]: r["name"] for r in xc["targets"]}


def sign_test_p(neg, k):
    """Exact one-sided binomial P(X >= neg | p=0.5, n=k)."""
    return sum(math.comb(k, i) for i in range(neg, k + 1)) / 2 ** k


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp", **ENC)
    tmp.write(text)
    tmp.close()
    # Atomically replace the destination so an interrupted write
    # cannot leave a partial output file.
    os.replace(tmp.name, path)


def analyze(net, artifacts_dir=None):
    snaps = load_schedule_snapshots(net)
    live = None
    census = load_census()
    tnames = load_target_names()

    src = Path(artifacts_dir) if artifacts_dir else datadir_for(net)
    seen, rows, skip_counts = set(), [], {"no_slot": 0, "signature_missing": 0, "block_unavailable": 0}
    for vf in sorted(src.glob("run-*.verify.json")):
        d = json.loads(vf.read_text(**ENC))
        sr = d.get("skip_reasons")
        if sr is None:
            # Legacy artifacts lack skip_reasons; their skipped rows represent
            # transactions that never landed — book them as no_slot
            skip_counts["no_slot"] += d.get("skipped", 0)
        else:
            for k in skip_counts:
                skip_counts[k] += sr.get(k, 0)
        for r in d.get("rows", []):
            if r.get("sig") in seen:
                continue
            seen.add(r["sig"])
            rows.append(r)
    if not rows:
        return None, f"no verified rows in {src} — run verify.py first (skip counts: {skip_counts})"

    def leader_of(slot):
        nonlocal live
        for (lo, hi), slot2id in snaps.items():
            if lo <= slot < hi:
                return slot2id.get(slot)
        if live is None:
            ep = rpc_read(net, "getEpochInfo")
            sched = rpc_read(net, "getLeaderSchedule", [{"commitment": "confirmed"}])
            lo = ep["absoluteSlot"] - ep["slotIndex"]
            live = {lo + off: ident for ident, offs in sched.items() for off in offs}
        return live.get(slot)

    for r in rows:
        ident = leader_of(r["slot"])
        r["leader_identity"] = ident
        r["leader_name"] = tnames.get(ident, (ident[:8] + "…" if ident else "unattributed"))
        ver, jito = census.get(ident, ("?", None)) if ident else ("?", None)
        r["version"] = ver
        r["registry_is_jito"] = jito  # registry flag of the producer; transport = public RPC
        r["run"] = r.get("tag", "?").split("|")[0]

    leaders = defaultdict(list)
    for r in rows:
        if r["leader_identity"]:
            leaders[r["leader_identity"]].append(r)

    try:
        src_label = Path(src).resolve().relative_to(HERE).as_posix()
    except ValueError:
        src_label = str(src)
    report = {"net": net, "source": src_label, "unique_rows": len(rows),
              "attributed": sum(1 for r in rows if r["leader_identity"]),
              "skip_counts": skip_counts, "leaders": {}, "families": {}}
    for ident, rs in leaders.items():
        by_run = defaultdict(list)
        for r in rs:
            by_run[r["run"]].append(r)
        entry = {"name": tnames.get(ident, ident[:8] + "…"), "n_total": len(rs),
                 "version": rs[0]["version"],
                 "registry_is_jito": rs[0]["registry_is_jito"], "runs": {}}
        for run, run_rows in sorted(by_run.items()):
            by_slot = defaultdict(list)
            for r in run_rows:
                by_slot[r["slot"]].append(r)
            fee_rec, send_rec, sendA_rec = [], [], []
            for slot, srows in sorted(by_slot.items()):
                if len(srows) < 5:
                    continue
                pos = lambda r: r["position_among_ok"] if r.get("position_among_ok") is not None else r["block_position"]
                order = sorted(srows, key=lambda r: r.get("send_mono_ns", 0))
                probes = sorted({r.get("probe", "?") for r in srows})
                rho_s = spearman(list(range(len(order))), [pos(r) for r in order])
                if rho_s is not None:
                    rec = {"run": run, "slot": slot, "n": len(srows), "probes": probes, "rho": round(rho_s, 4)}
                    send_rec.append(rec)
                    if probes == ["A"]:  # arrival proxy: A-ONLY groups
                        sendA_rec.append(rec)
                if sum(1 for r in srows if r.get("fee_extra_ul")) >= 3:
                    rho_f = spearman([r["fee_extra_ul"] for r in srows], [pos(r) for r in srows])
                    if rho_f is not None:
                        fee_rec.append({"run": run, "slot": slot, "n": len(srows), "probes": probes, "rho": round(rho_f, 4)})
            entry["runs"][run] = {"n": len(run_rows), "slots": sorted(by_slot),
                                  "rho_send_records": send_rec, "rho_sendA_records": sendA_rec,
                                  "rho_fee_records": fee_rec}
        cp = defaultdict(dict)
        for r in rs:
            if r.get("probe") == "C":
                cp[(r["run"], r.get("pair"))][r.get("side")] = r  # Include the run ID because pair numbers restart in each run.
        if cp:
            first_sent_won = sum(1 for sides in cp.values()
                                 if sides.get("d1", {}).get("sig_was_ok") and not sides.get("d2", {}).get("sig_was_ok"))
            entry["c_pairs"] = {"pairs": len(cp), "first_sent_won": first_sent_won}
        report["leaders"][ident] = entry

    # family aggregates — auto signs/means/p, nothing hand-counted
    fams = defaultdict(lambda: {"leaders": set(), "txs": 0, "fee": [], "sendA": [], "send_mixed": []})
    for ident, e in report["leaders"].items():
        if e["n_total"] < 5:
            continue
        key = (e["version"], bool(e["registry_is_jito"]))
        f = fams[key]
        f["leaders"].add(ident)
        f["txs"] += e["n_total"]
        for run in e["runs"].values():
            f["fee"] += [x["rho"] for x in run["rho_fee_records"]]
            f["sendA"] += [x["rho"] for x in run["rho_sendA_records"]]
            f["send_mixed"] += [x["rho"] for x in run["rho_send_records"] if x["probes"] != ["A"]]
    pool_fee = []
    for key, f in fams.items():
        neg = sum(1 for x in f["fee"] if x < 0)
        f_out = {"leaders": len(f["leaders"]), "txs": f["txs"],
                 "fee_rho": {"k": len(f["fee"]), "negative": neg,
                             "mean": round(sum(f["fee"]) / len(f["fee"]), 4) if f["fee"] else None,
                             "sign_p": round(sign_test_p(neg, len(f["fee"])), 4) if f["fee"] else None},
                 "sendA_rho": {"k": len(f["sendA"]),
                               "mean": round(sum(f["sendA"]) / len(f["sendA"]), 4) if f["sendA"] else None},
                 "send_mixed_descriptive": {"k": len(f["send_mixed"]),
                                            "mean": round(sum(f["send_mixed"]) / len(f["send_mixed"]), 4) if f["send_mixed"] else None}}
        pool_fee += f["fee"]
        report["families"][f"{key[0]}|{'JITO' if key[1] else 'TPU'}"] = f_out
    neg = sum(1 for x in pool_fee if x < 0)
    report["families"]["POOLED"] = {"fee_rho": {"k": len(pool_fee), "negative": neg,
                                                "mean": round(sum(pool_fee) / len(pool_fee), 4) if pool_fee else None,
                                                "sign_p": round(sign_test_p(neg, len(pool_fee)), 4) if pool_fee else None}}
    return report, None


def to_md(rep):
    L = ["# Inside the Block — analysis table (auto-generated; all stats computed from the listed records)", "",
         f"source: {rep['source']} · unique txs: {rep['unique_rows']} · attributed: {rep['attributed']} "
         f"· leaders: {len(rep['leaders'])} · skip counts: {rep['skip_counts']}", "",
         "fee-rho / sendA-rho: per-slot Spearman inside single blocks (slot n≥5). sendA = arrival proxy from "
         "A-ONLY groups; mixed groups are descriptive only. registry_is_jito = validator-registry flag of the "
         "producer (our transport is public RPC; no relay-path comparison is claimed). "
         "k = slot-level correlations; neg counts negative ones; mean is their average; "
         "sign-p = exact one-sided sign-test p-value.", "",
         "| family | leaders with ≥5 tx | txs | fee-ρ neg/k (mean, sign-p) | sendA-ρ k (mean) | mixed-ρ k (mean, descr.) |",
         "|---|---|---|---|---|---|"]
    for name, f in sorted(rep["families"].items()):
        fee, sa, mx = f["fee_rho"], f.get("sendA_rho"), f.get("send_mixed_descriptive")
        esc = name.replace("|", r"\|")  # Escape literal pipes in family labels so they stay in one Markdown cell
        if name == "POOLED":
            L.append(f"| **{esc}** | — | — | **{fee['negative']}/{fee['k']} ({fee['mean']}, p={fee['sign_p']})** | — | — |")
        else:
            L.append(f"| {esc} | {f['leaders']} | {f['txs']} | {fee['negative']}/{fee['k']} ({fee['mean']}, p={fee['sign_p']}) | "
                     f"{sa['k']} ({sa['mean']}) | {mx['k']} ({mx['mean']}) |")
    L += ["", "Per-leader records (run, slot, n, probes, rho) live in analysis JSON; raw sigs/slots in data/."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="mainnet")
    ap.add_argument("--artifacts", default=None, help="verify-artifacts dir (default: data/<net>; e.g. verify-out for fresh judge output)")
    ap.add_argument("--out", default="analysis-table.md")
    ap.add_argument("--json-out", default="analysis.json")
    args = ap.parse_args()
    rep, err = analyze(args.net, args.artifacts)
    if err:
        sys.exit(err)
    atomic_write(HERE / args.json_out, json.dumps(rep, indent=1, ensure_ascii=False))
    atomic_write(HERE / args.out, to_md(rep))
    print(to_md(rep))
    print(f"\n-> {args.json_out} + {args.out}")


if __name__ == "__main__":
    main()
