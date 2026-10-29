#!/usr/bin/env python3
"""ITB trickle — low-rate probes aimed at validators running less common client versions.

Purpose: land a handful of base-fee probes inside ONE window of a rare-client target
(e.g. agave-4.5.0-alpha) to obtain 3rd-version coverage. Public-relay latency is 8-30 s
bimodal, so instead of aiming we DRIP: start ~35 s before the window, send 2 tx/s of
plain base-fee memos, stop 5 s after the window end — the window is covered regardless
of which latency mode each tx draws. ~84 txs x 5000 lamports ≈ 0.0004 SOL per attempt.

Usage (from the repository root): python trickle.py --leader <identity-pubkey> --live [--net mainnet]
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "sender"))
from itb_sender import (rpc, rpc_read, load_key, memo_ix, cu_limit_ix, send_payload,  # noqa: E402
                        wait_for_slot, confirm_rows, BASE_FEE_LAMPORTS, RUN_TAG, NETS)
from solders.message import Message  # noqa: E402
from solders.transaction import Transaction  # noqa: E402
from solders.hash import Hash  # noqa: E402
from leader_watch import windows_for  # noqa: E402


def next_window(ident, min_ahead_s=25.0):
    ep = rpc_read("mainnet", "getEpochInfo")
    base, slot_now = ep["absoluteSlot"] - ep["slotIndex"], ep["absoluteSlot"]
    sched = rpc_read("mainnet", "getLeaderSchedule", [{"commitment": "confirmed"}])
    for a, b in windows_for(sorted(base + off for off in sched.get(ident, []))):
        if (a - slot_now) * 0.4 >= min_ahead_s and (b - a) >= 3:
            return a, b
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leader", required=True)
    ap.add_argument("--net", default="mainnet", choices=NETS)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--key-file", default="keys/mainnet.json")
    ap.add_argument("--rate-per-s", type=float, default=2.0)
    ap.add_argument("--spend-cap-sol", type=float, default=0.0006, help="maximum total base-fee spending in SOL")
    ap.add_argument("--max-tx", type=int, default=120, help="hard transaction-count cap, independent of the send rate")
    args = ap.parse_args()

    class A:
        key_file = args.key_file
    key = load_key(A())
    payer = key.pubkey()

    w = next_window(args.leader)
    if not w:
        sys.exit("no reachable window for this leader in the current epoch")
    wa, wb = w
    print(f"leader {args.leader[:8]}… window {wa}-{wb}; drip from {wa}-{35:.0f}s to {wb}+5s @ {args.rate_per_s}/s")
    if not args.live:
        print("DRY: would send ~%d txs (~%d lamports)" % (int((wb - wa + 100) * 0.4 * args.rate_per_s),
                                                          int((wb - wa + 100) * 0.4 * args.rate_per_s) * BASE_FEE_LAMPORTS))
        return

    out = Path("data") / args.net / f"run-{RUN_TAG}-trickle.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    wait_for_slot(args.net, wa, 35000)
    bh = Hash.from_string(rpc_read(args.net, "getLatestBlockhash")["value"]["blockhash"])
    t0 = time.monotonic_ns()
    drip_s = (wb - wa + 1) * 0.4 + 40.0  # window span + 35s pre + 5s post, WALL CLOCK
    i = 0
    max_tx = min(int(drip_s * args.rate_per_s), args.max_tx,
                 int(args.spend_cap_sol * 1e9 / 5000))  # count and fee caps are both enforced; the send rate cannot bypass either
    while time.monotonic() - t0 / 1e9 < drip_s and i < max_tx:
        tx = Transaction([key], Message.new_with_blockhash(
            [cu_limit_ix(20_000), memo_ix(f"{RUN_TAG}|TRK|{i:04d}")], payer, bh), bh)
        row = {"probe": "T", "seq": i, "tag": f"{RUN_TAG}|TRK|{i:04d}", "sig": str(tx.signatures[0]),
               "send_mono_ns": time.monotonic_ns() - t0,
               "send_wall": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        try:
            rpc(args.net, "sendTransaction", send_payload(tx))
            row["sent"] = True
        except Exception as e:
            row["sent"] = False
            row["error"] = str(e)[:100]
        rows.append(row)
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        i += 1
        if i % 10 == 0:
            bh = Hash.from_string(rpc_read(args.net, "getLatestBlockhash")["value"]["blockhash"])  # keep fresh
        time.sleep(1.0 / args.rate_per_s)
    print(f"sent {len(rows)} ({sum(1 for r in rows if r.get('sent'))} acked); confirming...")
    confirm_rows(args.net, rows, sleep_s=20)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    landed = sorted(r["slot"] for r in rows if r.get("slot"))
    inside = [s for s in landed if wa <= s <= wb + 3]
    print(f"landed {len(landed)}/{len(rows)} | in leader window {wa}-{wb}: {len(inside)} | slots {landed[:8]}…")
    print(f"raw -> {out}")


if __name__ == "__main__":
    main()
