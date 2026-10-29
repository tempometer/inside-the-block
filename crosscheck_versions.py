#!/usr/bin/env python3
"""ITB version cross-check — 2nd source for the 8 target leaders' client versions.

Source 1 (StakeWiz registry) is recorded in p0-target-shortlist.json. This script
is source 2: the chain itself via public RPC gossip views —
  getVoteAccounts (votePubkey -> nodePubkey)  +  getClusterNodes (nodePubkey -> version).
Both StakeWiz and gossip ultimately reflect the node's self-reported version string,
but they are independent observations through different infrastructure; agreement is
the cross-check. The registry's is_jito flag has no independent second source in this
dataset (Jito endpoints were unreachable from our network) — see README.md, Limitations.

Usage: python crosscheck_versions.py   (writes p0-version-crosscheck.json)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "sender"))
from itb_sender import rpc  # noqa: E402

HERE = Path(__file__).parent
# eight selected targets: (shortlist name-prefix match, expected client family)
CHOSEN = [
    ("Figment", "agave-4.3.2|JITO"),
    ("Helius", "agave-4.3.2|JITO"),
    ("Jupiter", "FD-date-version|JITO"),
    ("Everstake", "FD-date-version|JITO"),
    ("7QQGNm3p", "agave-4.3.2|TPU"),
    ("5Us18hLZ", "agave-4.3.2|TPU"),
    ("pebb1eQX", "FD-date-version|TPU"),   # Pebble — rarest combo
    ("sce3TfT8", "agave-4.5.0-alpha|TPU"),
]


def load_targets():
    """Resolve full pubkeys from the shortlist artifact by prefix — never from memory."""
    short = json.loads((HERE / "p0-target-shortlist.json").read_text())
    flat = [v for lst in short.values() for v in lst]
    targets = {}
    for prefix, fam in CHOSEN:
        m = [v for v in flat if v["identity"].startswith(prefix) or v["name"] == prefix]
        if len(m) != 1:
            sys.exit(f"target {prefix}: expected 1 shortlist match, got {len(m)}")
        name = m[0]["name"] or f"anon-{prefix}"
        targets[m[0]["identity"]] = (name, f"{fam}|ver={m[0]['ver']}")
    return targets


def family_from_version(v: str, is_fd_claim: bool) -> str:
    if v.startswith("agave"):
        return f"agave-{v.split('agave-')[-1]}"
    return f"{'FD' if is_fd_claim else '?'}-{v}"


def main():
    va = rpc("mainnet", "getVoteAccounts")
    vote2node = {v["votePubkey"]: v["nodePubkey"] for grp in ("current", "delinquent") for v in va.get(grp, [])}
    nodes = rpc("mainnet", "getClusterNodes")
    node2ver = {n["pubkey"]: (n.get("version"), n.get("featureSet")) for n in nodes}

    out, agree, disagree, missing = [], 0, 0, 0
    for pk, (name, claim) in load_targets().items():
        node_pk = vote2node.get(pk, pk)  # shortlist pubkeys are vote accounts; fall back to identity
        ver, feat = node2ver.get(node_pk, (None, None))
        row = {"pubkey": pk, "name": name, "node_pubkey": node_pk,
               "stakewiz_family": claim, "gossip_version": ver, "feature_set": feat}
        if ver is None:
            row["crosscheck"] = "NOT-IN-GOSSIP (node down or rpc view gap)"
            missing += 1
        else:
            sw_ver = claim.rsplit("ver=", 1)[-1]
            row["crosscheck"] = "AGREE" if sw_ver == ver else f"DISAGREE (stakewiz {sw_ver} vs gossip {ver})"
            agree += row["crosscheck"] == "AGREE"
            disagree += row["crosscheck"].startswith("DISAGREE")
        out.append(row)
        print(f"{name:10} {claim:38} gossip={ver or '—':16} {row['crosscheck']}")

    summary = {"date": "2026-10-26", "rpc": "api.mainnet-beta.solana.com (public)",
               "method": "getVoteAccounts -> nodePubkey -> getClusterNodes version",
               "agree": agree, "disagree": disagree, "not_in_gossip": missing, "targets": out,
               "note": "gossip version = node self-report observed by public RPC; independent of "
                       "StakeWiz infrastructure but same underlying self-reporting. is_jito remains "
                       "single-source (StakeWiz) — Jito endpoints DNS-blocked from this machine."}
    (HERE / "p0-version-crosscheck.json").write_text(json.dumps(summary, indent=1))
    print(f"\ncross-check: {agree} agree / {disagree} disagree / {missing} not-in-gossip "
          f"-> p0-version-crosscheck.json")


if __name__ == "__main__":
    main()
