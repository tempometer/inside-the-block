#!/usr/bin/env python3
"""ITB leader watch — lists upcoming leader windows for probe scheduling.

Pure read-only RPC. Prints upcoming leader windows (consecutive slot runs) for the
8 ITB targets on mainnet, or for any/one leader on devnet (dress-rehearsal mode).
Output feeds itb_sender.py --aim-slot <absolute slot>.

Usage:
  python leader_watch.py --net mainnet                 # 8 targets, next 3 windows each
  python leader_watch.py --net mainnet --json
  python leader_watch.py --net devnet --any            # next window of any leader
  python leader_watch.py --net devnet --leader <identity-pubkey> --windows 5

Gotchas honored (26.10): getLeaderSchedule slots are EPOCH-RELATIVE offsets
(absolute = epoch_start + offset); schedule keys are identity/node pubkeys,
NOT vote pubkeys (map via getVoteAccounts).
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "sender"))
from itb_sender import rpc, NETS  # noqa: E402

HERE = Path(__file__).parent
SLOT_MS = 400


def epoch_base(net):
    ep = rpc(net, "getEpochInfo")
    return ep["absoluteSlot"] - ep["slotIndex"], ep["absoluteSlot"], ep["epoch"]


def windows_for(slots):
    """Group sorted absolute slots into consecutive windows [(start, end_inclusive), ...]."""
    wins, start, prev = [], None, None
    for s in slots:
        if start is None:
            start = prev = s
        elif s == prev + 1:
            prev = s
        else:
            wins.append((start, prev))
            start = prev = s
    if start is not None:
        wins.append((start, prev))
    return wins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="mainnet", choices=NETS)
    ap.add_argument("--leader", help="identity pubkey (single leader mode)")
    ap.add_argument("--any", action="store_true", help="any leader: next window overall")
    ap.add_argument("--windows", type=int, default=3, help="windows per leader to show")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base, slot_now, epoch = epoch_base(args.net)
    sched = rpc(args.net, "getLeaderSchedule", [{"commitment": "confirmed"}])

    if args.net == "mainnet" and not args.leader:
        import crosscheck_versions as cc
        va = rpc("mainnet", "getVoteAccounts")
        vote2node = {v["votePubkey"]: v["nodePubkey"] for g in ("current", "delinquent") for v in va.get(g, [])}
        targets = {vote2node.get(pk, pk): name for pk, (name, _) in cc.load_targets().items()}
    elif args.leader:
        targets = {args.leader: args.leader[:8]}
    else:
        targets = {pk: pk[:8] for pk in sched}

    table = []
    for ident, name in targets.items():
        wins = windows_for(sorted(base + off for off in sched.get(ident, [])))
        upcoming = [(a, b) for a, b in wins if b >= slot_now][:args.windows]
        if not upcoming:
            continue
        for a, b in upcoming:
            table.append({"leader": name, "identity": ident, "window_start": a, "window_end": b,
                          "slots": b - a + 1,
                          "starts_in_s": round((a - slot_now) * SLOT_MS / 1000, 1)})
    table.sort(key=lambda r: r["window_start"])

    if args.any:
        table = table[:1]
    if args.json:
        print(json.dumps({"epoch": epoch, "slot_now": slot_now, "windows": table}, indent=1))
        return
    print(f"epoch {epoch} | slot now {slot_now} | slot=400ms")
    for r in table:
        print(f"  {r['leader']:12} {r['window_start']}-{r['window_end']} ({r['slots']} slots) "
              f"starts in {r['starts_in_s']:8.1f}s")


if __name__ == "__main__":
    main()
