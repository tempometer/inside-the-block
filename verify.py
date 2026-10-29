#!/usr/bin/env python3
"""ITB verify — recompute probe tx positions straight from the chain (Verifiability, 25% of score).

Reads sender JSONL run logs, fetches getBlock(slot) from ANY Solana RPC (default public
devnet; --rpc to point at an archive node), locates each of OUR signatures inside its
block's transaction order, and writes ONE verification artifact PER INPUT FILE (a single merged artifact
would cause downstream double-counting). Each artifact contains per row:
  - absolute position in block + position among successful txs + on-chain err
  - the actual on-chain fee paid (meta.fee), for auditing transaction costs
  - Spearman rho(send order, position) per run + probe C conflict outcomes (display)

Judges: `python verify.py --net mainnet data/mainnet/*.jsonl` recomputes every position
and fee from raw signatures+slots alone — no trust in our analysis needed.

Usage:
  python verify.py --net devnet data/devnet/run-20261026-*-probeA.jsonl
  python verify.py --net mainnet data/mainnet/*.jsonl
  python verify.py --rpc https://<archive-rpc> data/mainnet/*.jsonl
"""
import argparse
import glob as globmod
import json
import math
import sys
import time
from pathlib import Path

NETS = {"devnet": "https://api.devnet.solana.com", "mainnet": "https://api.mainnet-beta.solana.com"}


def rpc(url, method, params=None, timeout=30, retries=2):
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(f"{method}: {d['error']}")
            return d["result"]
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


FALLBACK_RPC = {"https://solana-rpc.publicnode.com": "https://api.mainnet-beta.solana.com"}


def load_block(url, slot, cache):
    """Full tx-details block; cached per slot. Falls back to the primary RPC when the
    read endpoint throttles getBlock (84-slot runs overwhelmed publicnode, 27.10)."""
    if slot not in cache:
        try:
            cache[slot] = rpc(url, "getBlock", [slot, {
                "maxSupportedTransactionVersion": 1, "transactionDetails": "full", "rewards": False}])
        except Exception:
            fb = FALLBACK_RPC.get(url)
            if not fb:
                raise
            cache[slot] = rpc(fb, "getBlock", [slot, {
                "maxSupportedTransactionVersion": 1, "transactionDetails": "full", "rewards": False}])
        time.sleep(0.25)
    return cache[slot]


def locate(block, sig):
    """Return (position, position_ok, err) for sig inside block's tx order, or None."""
    for i, t in enumerate(block["transactions"]):
        if sig in t["transaction"]["signatures"]:
            err = t["meta"]["err"]
            return i, err is None, (json.dumps(err) if err else None)
    return None


def ranks(xs):
    """Average ranks (ties share the mean) — for Spearman without scipy."""
    out, ordered = [0.0] * len(xs), sorted(range(len(xs)), key=lambda i: xs[i])
    for k, i in enumerate(ordered):
        out[i] += k + 1
    seen = {}
    for i, x in enumerate(xs):
        seen.setdefault(x, []).append(i)
    for idxs in seen.values():
        if len(idxs) > 1:
            m = sum(out[i] for i in idxs) / len(idxs)
            for i in idxs:
                out[i] = m
    return out


def spearman(a, b):
    if len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


SKIP_REASONS = ("no_slot", "block_unavailable", "signature_missing")


def verify_file(f, url, block_cache, out_dir):
    """Verify ONE run file -> its own .verify.json artifact.
    Returns (rows, reasons, n_input). Skip reasons are counted separately so an
    incomplete replay can never pass as a complete one:
      no_slot            input row has no slot/sig on record (never landed) — expected losers
      block_unavailable  RPC could not serve the block (pruned/throttled) — verification gap
      signature_missing  row claims slot+sig but the sig is not in that block — gap
    """
    frows, n_input = [], 0
    reasons = {k: 0 for k in SKIP_REASONS}
    for r in [json.loads(l) for l in Path(f).read_text().splitlines() if l.strip()]:
        n_input += 1
        if not r.get("slot") or not r.get("sig"):
            reasons["no_slot"] += 1
            continue
        try:
            blk = load_block(url, r["slot"], block_cache)
        except Exception as e:
            print(f"WARN block {r['slot']} unavailable ({str(e)[:80]}) — row skipped", file=sys.stderr)
            reasons["block_unavailable"] += 1
            continue
        hit = locate(blk, r["sig"])
        if hit is None:
            print(f"WARN {r['sig'][:16]}… not found in block {r['slot']} — row skipped", file=sys.stderr)
            reasons["signature_missing"] += 1
            continue
        pos, ok, err = hit
        r2 = dict(r)
        r2.update(block_position=pos, block_n_txs=len(blk["transactions"]),
                  position_among_ok=None, onchain_err=err)
        frows.append(r2)
    # positions-among-ok + on-chain fee: one pass per distinct slot of THIS file
    by_slot = {}
    for r2 in frows:
        by_slot.setdefault(r2["slot"], []).append(r2)
    for slot, rs in by_slot.items():
        blk = block_cache[slot]
        ok_sigs, fee_of, n_ok_before, run = set(), {}, {}, 0
        for t in blk["transactions"]:
            sig0 = t["transaction"]["signatures"][0]
            fee_of[sig0] = t["meta"]["fee"]
            if t["meta"]["err"] is None:
                ok_sigs.add(sig0)
                n_ok_before[sig0] = run
                run += 1
        for r2 in rs:
            r2["position_among_ok"] = n_ok_before.get(r2["sig"])
            r2["block_n_ok_txs"] = run
            r2["sig_was_ok"] = r2["sig"] in ok_sigs
            r2["fee_lamports"] = fee_of.get(r2["sig"])
    out = out_dir / (Path(f).stem + ".verify.json")
    gaps = reasons["block_unavailable"] + reasons["signature_missing"]
    out.write_text(json.dumps({"run": Path(f).name, "rows": frows,
                               "input_rows": n_input, "verified": len(frows),
                               "skipped": sum(reasons.values()), "skip_reasons": reasons,
                               "complete": bool(frows) and gaps == 0}, indent=1))
    return frows, reasons, n_input


def display(f, frows):
    print(f"\n== {Path(f).name}: {len(frows)} txs verified from chain ==")
    hdr = f"{'probe':5} {'seq/pair':8} {'slot':11} {'pos':>5} {'pos_ok':>6} {'n_tx':>5} {'fee':>7} {'err':<10} tag"
    print(hdr)
    for r in sorted(frows, key=lambda x: (str(x.get("probe")), x.get("seq", x.get("pair", 0)), x.get("side", ""))):
        ident = str(r.get("seq", f"{r.get('pair','?')}{r.get('side','')}"))
        err = (r.get("onchain_err") or "-")[:10]
        print(f"{r.get('probe','?'):5} {ident:8} {r['slot']:<11} {r['block_position']:>5} "
              f"{str(r['position_among_ok']):>6} {r['block_n_txs']:>5} {str(r.get('fee_lamports','?')):>7} {err:<10} {r.get('tag','')}")
    send_order = sorted(frows, key=lambda r: r.get("send_mono_ns", 0))
    rho = spearman(list(range(len(send_order))), [r["block_position"] for r in send_order])
    fees = sum(r.get("fee_lamports") or 0 for r in frows)
    print(f"  spearman(send_order, position)={rho if rho is None else round(rho, 4)} | "
          f"total on-chain fee: {fees} lamports")
    pairs = {}
    for r in frows:
        if r.get("probe") == "C":
            pairs.setdefault(r.get("pair"), {})[r.get("side")] = r
    if pairs:
        print("  conflict pairs: pair | winner | loser fate")
        for p, sides in sorted(pairs.items(), key=lambda kv: (kv[0] is None, kv[0])):
            win = next((s for s, r in sides.items() if r["sig_was_ok"]), None)
            if win:
                lose = sides.get("d2" if win == "d1" else "d1")
                fate = ("dropped (no slot on record)" if lose is None
                        else (f"failed-on-chain @{lose.get('slot')}" if lose.get("onchain_err")
                              else "BOTH SIDES LANDED"))
                print(f"    {p}: winner={win} loser={fate}")
            else:
                print(f"    {p}: NO landed side")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="sender JSONL run logs")
    ap.add_argument("--net", default="devnet", choices=NETS)
    ap.add_argument("--rpc", help="override RPC url (e.g. archive node)")
    ap.add_argument("--out-dir", default="verify-out",
                    help="artifacts go here, NEVER over the shipped *.verify.json (judges must not be able to destroy evidence by re-running)")
    args = ap.parse_args()
    url = args.rpc or NETS[args.net]

    expanded = []
    for pat in args.files:
        hit = globmod.glob(pat)
        expanded += hit if hit else [pat]
    args.files = expanded
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    block_cache = {}
    tot = {"input": 0, "verified": 0}
    tot.update({k: 0 for k in SKIP_REASONS})
    for f in args.files:
        frows, reasons, n_input = verify_file(f, url, block_cache, out_dir)
        display(f, frows)
        gaps = reasons["block_unavailable"] + reasons["signature_missing"]
        print(f"  artifact -> {Path(f).with_suffix('.verify.json').name} (verified {len(frows)}/{n_input}; "
              f"skips: no_slot={reasons['no_slot']} block_unavailable={reasons['block_unavailable']} "
              f"signature_missing={reasons['signature_missing']})")
        tot["input"] += n_input
        tot["verified"] += len(frows)
        for k in SKIP_REASONS:
            tot[k] += reasons[k]
    # Return a nonzero exit status when verification is incomplete.
    # no_slot rows are expected losers (never landed) and do NOT fail the run;
    # RPC gaps do. Zero verified rows at all is a hard failure.
    gaps = tot["block_unavailable"] + tot["signature_missing"]
    if tot["verified"] == 0:
        print(f"VERIFY FAILED: 0 of {tot['input']} rows verified "
              f"(skips: {', '.join(f'{k}={tot[k]}' for k in SKIP_REASONS)})", file=sys.stderr)
        sys.exit(1)
    if gaps:
        print(f"VERIFY INCOMPLETE: {tot['verified']}/{tot['input']} rows verified, "
              f"{gaps} lost to verification gaps "
              f"(block_unavailable={tot['block_unavailable']}, signature_missing={tot['signature_missing']})",
              file=sys.stderr)
        sys.exit(2)
    print(f"verify complete: {tot['verified']}/{tot['input']} rows verified from chain; "
          f"no-slot (never landed) rows: {tot['no_slot']}")


if __name__ == "__main__":
    main()
