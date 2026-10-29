#!/usr/bin/env python3
"""ITB campaign runner — coordinates mainnet probe runs for selected validators.
Real transactions require an explicit --live flag. The runner schedules composite
A/B probes per target window, then sequential conflict pairs; attribution is by the
actual landing slot. Progress and the cumulative spend ledger live in
data/<net>/campaign-state.json (resume-safe, hard spend cap); sweep_losers() recovers
remaining conflict-source balances. See README.md for the executed protocol.

Usage:
  python itb_campaign.py --status                          # ledger + progress, no sends
  python itb_campaign.py --live                            # full campaign (cap-guarded)
  python itb_campaign.py --live --only figment,helius      # subset
  python itb_campaign.py --live --dry                      # full walk, build-only, no sends
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "sender"))
from itb_sender import rpc, rpc_read, read_url, BASE_FEE_LAMPORTS  # noqa: E402
from leader_watch import windows_for, epoch_base  # noqa: E402
import crosscheck_versions as cc  # noqa: E402

STATE = (HERE / "sender" / "data") if (HERE / "sender").exists() else (HERE / "data")  # support either directory layout
SLOT_MS = 400
C_LADDER_EST = 40_000  # priority-fee bound per B run at the 10k CU limit
AMOUNT_LAMPORTS = 890_880  # conflict transfer: source created >= rent-exempt; dests pre-created


def est_lamports(probe, count=0, pairs=0):
    if probe in ("A", "D"):
        return count * BASE_FEE_LAMPORTS
    if probe == "B":
        return count * BASE_FEE_LAMPORTS + C_LADDER_EST
    if probe == "C":
        # fees only to the ledger; transfer float moves between our own accounts and is swept back
        return pairs * 3 * BASE_FEE_LAMPORTS + 2 * pairs * BASE_FEE_LAMPORTS
    if probe == "X":  # composite final: bracket A10+B10 (measured ~120-130k real)
        return 20 * BASE_FEE_LAMPORTS + 40_000
    return 0


def load_state(net):
    f = STATE / net / "campaign-state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        return json.loads(f.read_text())
    return {"cap_sol": 0.05, "spent_lamports_est": 0, "targets": {}}


def save_state(net, st):
    (STATE / net / "campaign-state.json").write_text(json.dumps(st, indent=1))


def identity_map():
    va = rpc_read("mainnet", "getVoteAccounts")
    vote2node = {v["votePubkey"]: v["nodePubkey"] for g in ("current", "delinquent") for v in va.get(g, [])}
    return {name: vote2node.get(pk, pk) for pk, (name, _) in cc.load_targets().items()}


def next_window(ident, min_ahead_s=25.0):
    for attempt in range(3):
        try:
            ep = rpc_read("mainnet", "getEpochInfo")
            base, slot_now = ep["absoluteSlot"] - ep["slotIndex"], ep["absoluteSlot"]
            sched = rpc_read("mainnet", "getLeaderSchedule", [{"commitment": "confirmed"}])
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)
    for a, b in windows_for(sorted(base + off for off in sched.get(ident, []))):
        if (a - slot_now) * SLOT_MS / 1000 >= min_ahead_s and (b - a) >= 3:
            return a, b
    return None


def run_probe(probe, net, aim_slot, live, count=0, pairs=0, dry=False):
    cmd = [sys.executable, "itb_sender.py", "probe", "--probe", probe, "--net", net,
           "--key-file", "keys/mainnet.json",
           "--aim-slot", str(aim_slot), "--leads", "22000,18000,14000,10000,6000"]
    if probe in ("A", "B", "D", "X"):
        cmd += ["--count", str(count), "--burst", str(min(count, 200))]
    else:
        cmd += ["--pairs", str(pairs), "--amount-lamports", str(AMOUNT_LAMPORTS)]
    if live and not dry:
        cmd.append("--live")
    # rare leaders have windows 20+ min ahead: allow the subprocess to wait them out
    cwd = HERE / "sender" if (HERE / "sender").exists() else HERE  # run from the directory holding itb_sender.py
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, cwd=str(cwd))
    print("    " + (r.stdout.strip()[-1500:] if r.stdout.strip() else "(no stdout)"))  # show the last 1,500 characters of child stdout
    if r.returncode:
        print("    STDERR:", r.stderr.strip().splitlines()[-1] if r.stderr else "?")
    return r.returncode == 0


def verify_run(net):
    """verify the newest run file of each probe present (chain recompute)."""
    files = sorted((STATE / net).glob("run-*-probe*.jsonl"))
    if not files:
        return
    try:
        r = subprocess.run([sys.executable, "verify.py", "--net", net, "--rpc", read_url(net)] + [str(f) for f in files[-1:]],
                           capture_output=True, text=True, timeout=900, cwd=str(HERE / "sender" if (HERE / "sender").exists() else HERE))
        print("    verify:", (r.stdout.strip().splitlines() or ["?"])[-1])
    except subprocess.TimeoutExpired:
        print("    verify: TIMEOUT — continuing; verification stays incomplete. Rerun verify.py for this run.")


def sweep_losers(net, payer_key_file="keys/mainnet.json"):
    """Drain leftover conflict-source balances back to the payer (float recovery)."""
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.transaction import Transaction
    from solders.hash import Hash
    from itb_sender import transfer_ix, send_payload, load_key
    class A:  # minimal args shim for load_key
        key_file = payer_key_file
    key = load_key(A())
    payer = key.pubkey()
    total = 0
    for kf in sorted((STATE.parent / "keys").glob("run-*-probeC-keys.json")):
        meta = json.loads(kf.read_text())
        for m in meta:
            try:
                b = rpc_read(net, "getBalance", [m["src"]])["value"]
            except Exception:
                continue
            if b > 2 * BASE_FEE_LAMPORTS:
                bh = Hash.from_string(rpc_read(net, "getLatestBlockhash")["value"]["blockhash"])
                src = Keypair.from_json(json.dumps(m["src_secret_json"]))
                tx = Transaction([src], Message.new_with_blockhash(
                    [__import__("itb_sender").cu_limit_ix(10_000), transfer_ix(src.pubkey(), payer, b - BASE_FEE_LAMPORTS)], src.pubkey(), bh), bh)
                rpc(net, "sendTransaction", send_payload(tx))
                total += b - BASE_FEE_LAMPORTS
                time.sleep(0.5)
    print(f"  sweep: recovered ~{total} lamports to payer" if total else "  sweep: nothing to recover")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", default="mainnet", choices=["mainnet", "devnet"])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true", help="walk the plan without sending")
    ap.add_argument("--only", help="comma-separated target names (default: all 8)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--min-ahead-s", type=float, default=25.0)
    ap.add_argument("--cap-sol", type=float, default=0.05)
    args = ap.parse_args()

    st = load_state(args.net)
    failed_probes = []
    st["cap_sol"] = args.cap_sol
    idents = identity_map()
    names = [n.strip() for n in args.only.split(",")] if args.only else list(idents)
    unknown = [n for n in names if n not in idents]
    if unknown:
        sys.exit(f"unknown targets: {unknown} (known: {list(idents)})")

    if args.status:
        print(f"cap {st['cap_sol']} SOL | est spent {st['spent_lamports_est']/1e9:.6f} SOL "
              f"| remaining {st['cap_sol'] - st['spent_lamports_est']/1e9:.6f} SOL")
        for t, d in st["targets"].items():
            print(f"  {t:10} windows_done={d.get('windows_done', [])} runs={len(d.get('runs', []))}")
        return

    # Relay-adaptive plan: per target, two composite X-salvos into two separate
    # windows; conflict pairs run globally afterwards as an unaimed sequential stream.
    # Attribution is post-hoc: landing slot -> leader schedule.
    for name in names:
        tstate = st["targets"].setdefault(name, {"windows_done": [], "runs": []})
        ident = idents[name]
        for round_i in range(1):
            if not args.dry and sum(1 for r in tstate["runs"] if r.startswith("X")) >= round_i + 1:
                continue  # resume marks apply to LIVE walks only; dry always builds
            w = None if args.dry else next_window(ident, args.min_ahead_s + 10 * round_i)
            if not args.dry and not w:
                print(f"{name}: no reachable window left this epoch — skip round {round_i}")
                break
            est_w = est_lamports("X")
            if st["spent_lamports_est"] + est_w > st["cap_sol"] * 1e9:
                print(f"{name}: budget guard — stopping")
                break
            wa = w or (0, 3)
            print(f"{name}: X salvo round {round_i+1} window {wa[0]}-{wa[1]}")
            ok_x = run_probe("X", args.net, wa[0], args.live, count=100, dry=args.dry)
            if not ok_x:
                failed_probes.append(f"X@{name}")
            if args.live and not args.dry and ok_x:
                tstate["runs"].append(f"X@{wa[0]}")
                tstate["windows_done"].append(wa[0])
                st["spent_lamports_est"] += est_w
                save_state(args.net, st)
                verify_run(args.net)
        print(f"{name} done; ledger {st['spent_lamports_est']/1e9:.6f}/{st['cap_sol']} SOL est")
        if args.live and not args.dry:
            time.sleep(75)  # inter-target cooldown: cumulative IP quota killed targets 4+ on 27.10 night

    # global conflict-pair trickle (unaimed; sequential; attribution via schedule)
    # SAFETY: every spend path keys on effective-live (args.live and not args.dry) —
    # a bare `python itb_campaign.py` must stay 100% dry (no ledger writes, no sweep).
    if args.live and not args.dry:
        est_c = est_lamports("C", pairs=6)
        if st["spent_lamports_est"] + est_c > st["cap_sol"] * 1e9:
            print(f"C-trickle: budget guard ({est_c} over cap) — skipped")
        else:
            print(f"C-trickle: 6 pairs, no aim, sequential")
            ok_c = (args.live and not args.dry) and run_probe("C", args.net, 0, args.live, pairs=6)
            if args.live and not args.dry and not ok_c:
                failed_probes.append("C-trickle")
            if ok_c:
                st["spent_lamports_est"] += est_c
                save_state(args.net, st)
                sweep_losers(args.net)
                verify_run(args.net)
    print("campaign walk complete")
    print(f"summary: {len(names)} targets walked, {len(failed_probes)} probe failures")
    if failed_probes:
        sys.exit(1)  # a failed child probe must produce a nonzero exit status


if __name__ == "__main__":
    main()
