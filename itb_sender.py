#!/usr/bin/env python3
"""ITB sender — probe transactions for the Inside the Block engagement.

Modes (listing requirements): dry-run is DEFAULT; live sending needs --live AND
respects a hard spend cap. Keys are loaded from a file or env var, never committed
(keys/ is gitignored). Every tx is logged with send-timing for jitter analysis.

Fire model (devnet calibration, 2026-10-26): sequential ack-wait submits cost ~120ms/tx and
stretch a burst over ~6 slots — too slow for a 4-slot leader window. Live bursts use
--burst N: prebuild ALL txs, submit concurrently (fire-and-forget), collect acks after.
--aim-slot S waits until ~lead-ms before slot S (getSlot(processed) polling) so the
salvo arrives inside a chosen leader window (window start from leader_watch.py).

Usage:
  python itb_sender.py keygen --key-file keys/devnet.json      # create keypair
  python itb_sender.py airdrop --net devnet                    # devnet only, 1 SOL
  python itb_sender.py probe --probe A --count 5               # DRY: build only
  python itb_sender.py probe --probe A --count 20 --net devnet --live
  python itb_sender.py probe --probe B --count 12 --net devnet --live   # fee gradient
  python itb_sender.py probe --probe C --pairs 6 --net devnet --live    # conflict pairs
  # Aimed burst: 20 transactions targeting a window starting at slot S
  python leader_watch.py --net devnet --any          # pick S
  python itb_sender.py probe --probe A --count 20 --live --burst 20 --aim-slot S --lead-ms 300
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from base64 import b64encode
from pathlib import Path

from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash

MEMO_PROG = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")  # spl-memo (verified live on devnet 24.10)
COMPUTE_BUDGET = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
NETS = {
    "devnet": "https://api.devnet.solana.com",
    "mainnet": "https://api.mainnet-beta.solana.com",
}
# public mainnet endpoints usable WITHOUT a key; each has its own per-IP quota (~20
# sendTransaction/burst lands; more gets 429'd or silently dropped — calibrated 26.10).
# publicnode 403s python-urllib UA (StakeWiz pattern) -> browser UA sent always.
EXTRA_ENDPOINTS = {"mainnet": ["https://solana-rpc.publicnode.com"]}
RPC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
BASE_FEE_LAMPORTS = 5000
import uuid
RUN_TAG = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]  # random suffix: same-second launches stay distinct


def rpc_url(url, method, params=None, timeout=30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": RPC_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise RuntimeError(f"{method}: {d['error']}")
    return d["result"]


def rpc(net, method, params=None, timeout=30):
    return rpc_url(NETS[net], method, params, timeout)


def net_endpoints(net):
    return [NETS[net]] + EXTRA_ENDPOINTS.get(net, [])


def read_url(net):
    """ALL reads go to the EXTRA endpoint when present (publicnode has its own quota) —
    the primary endpoint's per-IP budget is RESERVED for sendTransaction. 26.10 lesson:
    aim-polling + schedule + verify reads exhaust the primary quota and it then silently
    DROPS sends (acked-but-never-relayed)."""
    eps = EXTRA_ENDPOINTS.get(net)
    return eps[0] if eps else NETS[net]


def rpc_read(net, method, params=None, timeout=30):
    url = read_url(net)
    try:
        return rpc_url(url, method, params, timeout)
    except Exception:
        if url == NETS[net]:
            raise
        return rpc_url(NETS[net], method, params, timeout)


def load_key(args) -> Keypair:
    if os.environ.get("ITB_KEY"):
        return Keypair.from_base58_string(os.environ["ITB_KEY"])
    kf = Path(args.key_file)
    if not kf.exists():
        sys.exit(f"key file {kf} not found (run: keygen --key-file {kf})")
    return Keypair.from_json(kf.read_text())


def memo_ix(tag: str) -> Instruction:
    return Instruction(program_id=MEMO_PROG, accounts=[], data=f"ITB|{tag}".encode())


def cu_limit_ix(cus: int) -> Instruction:
    # SetComputeUnitLimit { u32 } — without an explicit limit, priority fees bill on
    # the default 200k-CU budget per transaction.
    data = bytes([2]) + cus.to_bytes(4, "little")
    return Instruction(program_id=COMPUTE_BUDGET, accounts=[], data=data)


def cu_price_ix(microlamports: int) -> Instruction:
    # SetComputeUnitPrice { micro-lamports per CU }
    data = bytes([3]) + microlamports.to_bytes(8, "little")
    return Instruction(program_id=COMPUTE_BUDGET, accounts=[], data=data)


SYSTEM_PROG = Pubkey.from_string("11111111111111111111111111111111")


def transfer_ix(src: Pubkey, dst: Pubkey, lamports: int) -> Instruction:
    data = (2).to_bytes(4, "little") + lamports.to_bytes(8, "little")  # System Transfer
    return Instruction(program_id=SYSTEM_PROG, accounts=[
        AccountMeta(pubkey=src, is_signer=True, is_writable=True),
        AccountMeta(pubkey=dst, is_signer=False, is_writable=True),
    ], data=data)


def wait_funded(net, addr: str, need: int, timeout_s: int = 60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            if rpc_read(net, "getBalance", [addr])["value"] >= need:
                return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def wait_for_slot(net, aim_slot: int, lead_ms: int):
    """Predictive approach: slot time is 400ms nominal — poll getSlot at most 3 times
    (polling every 0.4s burned the RPC quota and got our salvos silently dropped 26.10)."""
    while True:
        now_slot = rpc_read(net, "getSlot", [{"commitment": "processed"}])
        ms_ahead = (aim_slot - now_slot) * 400
        if ms_ahead <= lead_ms:
            return
        sleep_s = min(max((ms_ahead - lead_ms - 500) / 1000, 1.5), 30)
        time.sleep(sleep_s)


def send_payload(tx):
    return [b64encode(bytes(tx)).decode(), {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]


def fire_burst(net, prepared, t0, max_workers=10, pace_ms=0):
    """Rate-disciplined salvo across ALL endpoints of the net (round-robin): each public
    endpoint lands ~10-20 sendTransaction per burst (calibrated 26.10: 10/10 and 5/5
    land; a 152-storm gets 429s AND silently drops the 'acked' tail). Pool 10/endpoint."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    eps = net_endpoints(net)
    out = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(2, max_workers * len(eps))) as ex:
        futs = []
        for i, (tx, row) in enumerate(prepared):
            if pace_ms and i >= max_workers * len(eps):
                time.sleep(pace_ms / 1000)
            row["send_mono_ns"] = time.monotonic_ns() - t0
            row["endpoint"] = eps[i % len(eps)]
            ts = time.monotonic_ns()
            futs.append((ex.submit(rpc_send_with_retry, eps[i % len(eps)], tx), row, ts))
        for fut, row, ts in futs:
            try:
                fut.result(timeout=90)
                row["submit_ms"] = round((time.monotonic_ns() - ts) / 1e6, 1)
                row["sent"] = True
            except Exception as e:
                row["sent"] = False
                row["error"] = str(e)[:120]
            with lock:
                out.append(row)
    return out


def rpc_send_with_retry(url, tx, retries=1):
    last = None
    for attempt in range(retries + 1):
        try:
            return rpc_url(url, "sendTransaction", send_payload(tx))
        except Exception as e:
            last = e
            if "429" in str(e) and attempt < retries:
                time.sleep(0.4)
                continue
            raise
    raise last


def confirm_rows(net, rows, sleep_s: int = 25):
    """Chunked inclusion poll: getSignatureStatuses in <=100-sig chunks with pacing
    (big single batch + per-sig polling both tripped public-RPC 429s on 26.10)."""
    time.sleep(sleep_s)
    pending = [r for r in rows if not r.get("slot")]
    for attempt in range(4):
        for i in range(0, len(pending), 100):
            chunk = pending[i:i + 100]
            try:
                st = rpc_read(net, "getSignatureStatuses", [[r["sig"] for r in chunk]])["value"]
            except Exception:
                time.sleep(8)
                continue
            for r, v in zip(chunk, st):
                if v and not r.get("slot"):
                    r["slot"] = v["slot"]
                    r["status"] = "ok" if v.get("confirmation_status") in ("confirmed", "finalized") else str(v)
            time.sleep(1.5)
        pending = [r for r in pending if not r.get("slot")]
        if not pending:
            return
        time.sleep(8)


def write_rows(out: Path, rows):
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def probe_conflicts(args, payer: Pubkey, key: Keypair, out: Path):
    """Probe C: per pair, a throwaway source funded EXACTLY transfer+fee, then two
    contenders (S->D1 vs S->D2, same amount) sent back-to-back -> mutually exclusive:
    whichever executes first empties S; the other must fail or be dropped."""
    pairs = args.pairs
    amount = args.amount_lamports
    est = pairs * (3 * BASE_FEE_LAMPORTS + amount)
    if args.live and est > int(args.spend_cap_sol * 1e9):
        sys.exit(f"spend cap exceeded: est {est} lamports > cap {args.spend_cap_sol} SOL")
    print(f"probe C pairs={pairs} amount={amount} net={args.net} live={args.live} est_lamports={est}")
    t0 = time.monotonic_ns()
    rows, sources, pair_meta = [], [], []
    for p in range(pairs):
        src, d1, d2 = Keypair(), Keypair(), Keypair()
        sources.append(src)
        pair_meta.append({"pair": p, "src": str(src.pubkey()), "d1": str(d1.pubkey()),
                          "d2": str(d2.pubkey()), "amount_lamports": amount,
                          "src_secret_json": json.loads(src.to_json()),
                          # Persist keys for every account that may receive funds
                          # BEFORE funding it — winners park the float on d1/d2.
                          "d1_secret_json": json.loads(d1.to_json()),
                          "d2_secret_json": json.loads(d2.to_json())})
    if args.live:
        # persist BEFORE funding: a lost key would strand the funded lamports (key files are excluded from version control)
        keysfile = out.parent.parent.parent / "keys" / (out.stem + "-keys.json")
        keysfile.parent.mkdir(parents=True, exist_ok=True)
        keysfile.write_text(json.dumps(pair_meta, indent=1))
        print(f"  throwaway keys persisted -> {keysfile.name}")
    else:
        keysfile = None  # dry runs keep keypairs in memory only, nothing persisted
        print("  dry: ephemeral pair keys, nothing persisted")
    for p, (src, meta) in enumerate(zip(sources, pair_meta)):
        if args.live:
            bh = Hash.from_string(rpc_read(args.net, "getLatestBlockhash")["value"]["blockhash"])
            fund = Transaction([key], Message.new_with_blockhash(
                [cu_limit_ix(20_000), transfer_ix(payer, Pubkey.from_string(meta["src"]), amount + BASE_FEE_LAMPORTS)], payer, bh), bh)
            rpc(args.net, "sendTransaction", send_payload(fund))
            time.sleep(1.2)
            if not wait_funded(args.net, meta["src"], amount + BASE_FEE_LAMPORTS):
                sys.exit(f"pair {p}: funding tx did not land in 60s — abort before contenders")
    print(f"  funding {'done' if args.live else 'skipped (dry)'}")

    if args.live and args.aim_slot:
        wait_for_slot(args.net, args.aim_slot, args.lead_ms + 1500)  # coarse approach, keep blockhash fresh
    bh = Hash.from_string(rpc_read(args.net, "getLatestBlockhash")["value"]["blockhash"])
    if args.live and args.aim_slot:
        wait_for_slot(args.net, args.aim_slot, args.lead_ms)         # final approach

    for p, (src, meta) in enumerate(zip(sources, pair_meta)):
        s = Pubkey.from_string(meta["src"])
        d1, d2 = Pubkey.from_string(meta["d1"]), Pubkey.from_string(meta["d2"])
        prepared = []
        for side, dst in (("d1", d1), ("d2", d2)):
            ixs = [cu_limit_ix(20_000), transfer_ix(s, dst, amount), memo_ix(f"{RUN_TAG}|PC{p:02d}{side}")]
            tx = Transaction([src], Message.new_with_blockhash(ixs, s, bh), bh)
            prepared.append((tx, {"probe": "C", "pair": p, "side": side,
                                  "tag": f"{RUN_TAG}|PC{p:02d}{side}", "sig": str(tx.signatures[0]),
                                  "src": meta["src"], "dst": meta[side], "amount_lamports": amount,
                                  "send_wall": datetime.datetime.now(datetime.timezone.utc).isoformat()}))
        if args.live and args.burst > 1:
            rows.extend(fire_burst(args.net, prepared, t0))
        else:
            for tx, row in prepared:
                row["send_mono_ns"] = time.monotonic_ns() - t0
                if args.live:
                    ts = time.monotonic_ns()
                    try:
                        rpc(args.net, "sendTransaction", send_payload(tx))
                        row["submit_ms"] = round((time.monotonic_ns() - ts) / 1e6, 1)
                        row["sent"] = True
                    except Exception as e:
                        row["sent"] = False
                        row["error"] = str(e)[:120]
                rows.append(row)
        for r in rows[-2:]:
            print(f"  [pair {p}/{r['side']}] live={r.get('sent', 'dry')} {r['tag']}")
    write_rows(out, rows)
    if args.live:
        print("confirming inclusion + conflict outcome (batched)...")
        confirm_rows(args.net, rows)
        dests = [m[k] for m in pair_meta for k in ("d1", "d2")]
        accts = rpc_read(args.net, "getMultipleAccounts", [dests, {"commitment": "confirmed"}])["value"]
        bal = {d: (a["lamports"] if a else 0) for d, a in zip(dests, accts)}
        for m in pair_meta:
            d1b, d2b = bal[m["d1"]], bal[m["d2"]]
            winner = "d1" if d1b >= m["amount_lamports"] else ("d2" if d2b >= m["amount_lamports"] else "none")
            for r in rows:
                if r.get("pair") == m["pair"]:
                    r["dst_balance_lamports"] = d1b if r["side"] == "d1" else d2b
            print(f"  pair {m['pair']}: winner={winner} d1={d1b} d2={d2b}")
        write_rows(out, rows)
        landed = sum(1 for m in pair_meta if max(bal[m["d1"]], bal[m["d2"]]) >= m["amount_lamports"])
        print(f"pairs with a landed winner: {landed}/{len(pair_meta)}; raw data -> {out}")


def probe_bursty(args, payer: Pubkey, key: Keypair, out: Path):
    """Probes A (identical fee burst), B (fee ladder anti-correlated), D (overload), or
    X = A+B+D composite battery fired as ONE salvo (separate subprocesses cannot share a
    1.6s window — each takes ~30s; the composite keeps the whole battery inside the aim).
    Prebuild everything, then sequential ack-wait or --burst salvo, aimed at --aim-slot."""
    cap_lam = int(args.spend_cap_sol * 1_000_000_000)
    if args.probe == "X":
        # Composite probe: 10 zero-fee (A) + 10 wide fee-ladder (B) transactions in
        # ONE aimed window. (Early "drops" turned out to be a missing --key-file — a
        # devnet key signing mainnet transactions; with the funded key they land fine.)
        subprobes = (["A"] * 10) + (["B"] * 10)
        # Real CU/tx is ~16k (not 1-2k) — low tiers give a 100x fee gradient at
        # <=1.6k lamports of priority fee each: detectable while limiting spend.
        bfees = [0, 1_000, 5_000, 25_000, 100_000, 250_000, 250_000, 100_000, 25_000, 5_000]
        fees = [0] * 10 + bfees
        args.count = len(fees)
    else:
        fees = [0] * args.count
        if args.probe == "B":
            ladder = [0, 1_000, 2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 2_500_000]
            fees = (ladder * (args.count // len(ladder) + 1))[:args.count]
        subprobes = [args.probe] * args.count
    # fee_extra_ul are MICROLAMPORTS/CU billed on the SetComputeUnitLimit value (20k; 10k failed simulation - memo+cu-ix consumes 11,295 CU)
    total_est = args.count * BASE_FEE_LAMPORTS + int(sum(fees) * 20_000 / 1e6)
    if args.live and total_est > cap_lam:
        sys.exit(f"spend cap exceeded: est {total_est} > cap {cap_lam}; raise nothing — redesign count")
    print(f"probe {args.probe} count={args.count} net={args.net} live={args.live} "
          f"burst={args.burst} aim_slot={args.aim_slot} leads={args.leads} est_lamports={total_est}")

    leads = [int(x) for x in args.leads.split(",")] if args.leads else [args.lead_ms]
    per = (args.count + len(leads) - 1) // len(leads) if len(leads) > 1 else args.count

    if args.live and args.aim_slot:
        wait_for_slot(args.net, args.aim_slot, max(leads) + 2000)  # coarse approach to earliest fire
    bh = Hash.from_string(rpc_read(args.net, "getLatestBlockhash")["value"]["blockhash"])

    t0 = time.monotonic_ns()
    prepared = []
    for i in range(args.count):
        tag = f"{RUN_TAG}|P{subprobes[i]}|{i:04d}"
        ixs = [cu_limit_ix(20_000)]  # explicit compute-unit limit on every transaction shape
        if fees[i]:
            ixs.append(cu_price_ix(fees[i]))
        ixs.append(memo_ix(tag))
        tx = Transaction([key], Message.new_with_blockhash(ixs, payer, bh), bh)
        prepared.append((tx, {"probe": subprobes[i], "seq": i, "tag": tag, "fee_extra_ul": fees[i],
                              "sig": str(tx.signatures[0]),
                              "send_wall": datetime.datetime.now(datetime.timezone.utc).isoformat()}))
    rows = []
    if not args.live:
        rows = [row for _, row in prepared]
        for row in rows:
            row["send_mono_ns"] = time.monotonic_ns() - t0
    elif args.aim_slot and len(leads) > 1:
        # bracket-spray: equal chunks at descending leads before the window — public-relay
        # fire->inclusion latency varies 10-25s (27.10), one lead can't guarantee landing
        # inside a 1.6s window; a bracket of salvos covers the jitter.
        for k, lead in enumerate(leads):
            chunk = prepared[k * per:(k + 1) * per] if k < len(leads) - 1 else prepared[k * per:]
            wait_for_slot(args.net, args.aim_slot, lead)
            print(f"  bracket salvo {k+1}/{len(leads)} lead={lead}ms n={len(chunk)}")
            rows += fire_burst(args.net, chunk, t0)
            if k + 1 < len(leads):
                # Preserve the configured time gaps between salvos; slot polling
                # granularity is too coarse for leads a few seconds apart.
                time.sleep(max(0.2, (lead - leads[k + 1]) / 1000 - 0.6))
    elif args.burst > 1:
        if args.aim_slot:
            wait_for_slot(args.net, args.aim_slot, leads[0])
        rows = fire_burst(args.net, prepared, t0)
    else:
        if args.aim_slot:
            wait_for_slot(args.net, args.aim_slot, leads[0])
        for tx, row in prepared:
            row["send_mono_ns"] = time.monotonic_ns() - t0
            ts = time.monotonic_ns()
            for attempt in range(2):
                try:
                    rpc(args.net, "sendTransaction", send_payload(tx))
                    row["submit_ms"] = round((time.monotonic_ns() - ts) / 1e6, 1)
                    row["sent"] = True
                    break
                except Exception as e:
                    row["sent"] = False
                    row["error"] = str(e)[:120]
                    if "429" in str(e) and attempt == 0:
                        time.sleep(2.0)
            time.sleep(1.2)  # sequential trickle must stay under the per-IP send rate
            rows.append(row)
    for r in rows:
        print(f"  [{r['seq']+1}/{args.count}] sent={r.get('sent', 'dry')} {r['tag']}")
    write_rows(out, rows)
    if args.live:
        print("confirming inclusion (batched)...")
        confirm_rows(args.net, rows)
        write_rows(out, rows)
        ok = sum(1 for r in rows if r.get("slot"))
        fire_span_ms = (rows[-1]["send_mono_ns"] - rows[0]["send_mono_ns"]) / 1e6 if len(rows) > 1 else 0
        print(f"included {ok}/{len(rows)}; fire span {fire_span_ms:.0f}ms; raw data -> {out}")
        if args.aim_slot:
            slots = sorted(r["slot"] for r in rows if r.get("slot"))
            if slots:
                inside = sum(1 for s in slots if args.aim_slot <= s <= args.aim_slot + 7)
                print(f"landing slots {slots[0]}..{slots[-1]} | inside aimed window(+7): {inside}/{len(slots)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["keygen", "airdrop", "probe"])
    ap.add_argument("--net", default="devnet", choices=NETS)
    ap.add_argument("--key-file", default="keys/devnet.json")
    ap.add_argument("--live", action="store_true", help="REAL sending (default: dry build)")
    ap.add_argument("--probe", choices=["A", "B", "C", "D", "X"], default="A")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--pairs", type=int, default=6, help="probe C: number of conflict pairs")
    ap.add_argument("--amount-lamports", type=int, default=1_000_000, help="probe C: transfer amount (>= rent-exempt)")
    ap.add_argument("--spend-cap-sol", type=float, default=0.05, help="hard cap for live runs")
    ap.add_argument("--burst", type=int, default=1, help=">1: fire-and-forget salvo with N threads")
    ap.add_argument("--aim-slot", type=int, default=0, help="absolute slot: fire ~lead-ms before it")
    ap.add_argument("--lead-ms", type=int, default=300, help="fire lead before aim-slot")
    ap.add_argument("--leads", default="", help="comma list of leads for bracket-spray (e.g. 26000,22000,18000,14000,10000)")
    args = ap.parse_args()

    if args.cmd == "keygen":
        kp = Keypair()
        Path(args.key_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.key_file).write_text(kp.to_json())
        print("keypair created:", args.key_file, "| pubkey:", kp.pubkey())
        return

    # dry-run stays runnable WITHOUT any key file (judge reproduction): ephemeral in-memory key
    if args.cmd == "probe" and not args.live and not Path(args.key_file).exists():
        key = Keypair()
        print(f"dry: ephemeral in-memory key {key.pubkey()} (build-only, not persisted)")
    else:
        key = load_key(args)
    payer = key.pubkey()

    if args.cmd == "airdrop":
        if args.net != "devnet":
            sys.exit("airdrop is devnet-only (money gate: no mainnet funding path)")
        sig = rpc(args.net, "requestAirdrop", [str(payer), 1_000_000_000])
        print("airdrop sig:", sig)

    if args.cmd == "probe":
        outdir = Path("data") / args.net
        outdir.mkdir(parents=True, exist_ok=True)
        out = outdir / f"run-{RUN_TAG}-probe{args.probe}.jsonl"
        if args.probe == "C":
            probe_conflicts(args, payer, key, out)
        else:
            probe_bursty(args, payer, key, out)


if __name__ == "__main__":
    main()
