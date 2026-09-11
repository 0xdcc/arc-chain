#!/usr/bin/env python3
"""Validate this planning package only; never invokes Git, RPC, project tests or services."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath

EXPECTED_ROLES={"M1", "M2", "M3", "M4"}
EXPECTED_SHA="13817f4e027375dd59cc7a202ae641c068525f53"

def issues(root: Path, manifest: dict | None = None, config: dict | None = None) -> list[str]:
    m=manifest or json.loads((root/"task-manifest.json").read_text(encoding="utf-8"))
    cfg=config or json.loads((root/"templates/arc-network.example.json").read_text(encoding="utf-8"))
    errors=[]
    ts=m.get("tasks",[]); by_id={t["id"]:t for t in ts}
    if len(ts)!=48 or len(by_id)!=48: errors.append("task count/uniqueness")
    if set(by_id)!={f"T{i:02d}" for i in range(1,49)}: errors.append("task ids")
    if set(m.get("main_brains",[]))!=EXPECTED_ROLES: errors.append("exactly four roles required")
    if m.get("upstream_sha")!=EXPECTED_SHA: errors.append("source sha drift")
    if m.get("max_children_per_brain")!=4 or m.get("absolute_max_children")!=16: errors.append("worker limits")
    if m.get("initial_global_worker_budget")!=sum(m["initial_children_by_brain"].values()): errors.append("initial budget mismatch")
    if any(v<1 or v>4 for v in m["initial_children_by_brain"].values()): errors.append("per-role initial slots")
    if m.get("sole_merge_owner")!="M1" or m.get("review_owner")!="M4":errors.append("authority separation")
    sp=m.get("source_policy",{})
    for key, expected in {"whole_repository_clean_required":False,"local_git_head_must_equal_code_reference":False,
                          "local_pool_data_may_differ":True,"data_only_change_blocks_arc":False,
                          "data_only_change_requires_global_reaudit":False,"blanket_data_directory_exemption":False,
                          "robinhood_catalog_to_arc_production":False}.items():
        if sp.get(key) is not expected:errors.append("source/data policy: "+key)
    if not (root/"07_本地池目录变化处理规则.md").is_file():errors.append("missing local catalog policy")
    colors={};path_owners={}
    def visit(k: str):
        if colors.get(k)==1: errors.append("dependency cycle: "+k); return
        if colors.get(k)==2:return
        colors[k]=1
        for d in by_id[k]["depends_on"]:
            if d not in by_id:errors.append("missing dependency: "+d)
            else:visit(d)
        colors[k]=2
    for t in ts:
        if t["lead"] not in EXPECTED_ROLES:errors.append("invalid owner: "+t["id"])
        if t.get("source_baseline_sha")!=EXPECTED_SHA:errors.append("ticket source drift: "+t["id"])
        if t.get("dispatch_ready") or t.get("baseline_sha") is not None:errors.append("seed cannot be authorized: "+t["id"])
        tp=root/t["ticket"]
        if not tp.is_file():errors.append("missing ticket: "+t["id"])
        else:
            txt=tp.read_text(encoding="utf-8")
            if EXPECTED_SHA not in txt or t["lead"] not in txt:errors.append("ticket provenance missing")
        for p in t["planned_write_paths"]:
            q=PurePosixPath(p)
            if q.is_absolute() or ".." in q.parts:errors.append("unsafe planned path: "+p)
            if p in path_owners and path_owners[p]!=t["lead"]:errors.append("cross-role file overlap: "+p)
            path_owners[p]=t["lead"]
        visit(t["id"])
    counts=Counter(t["lead"] for t in ts)
    if dict(counts)!={"M1":12,"M2":12,"M3":18,"M4":6}:errors.append("unexpected task distribution")
    for f in ["10_主脑1_总控与集成.md","20_主脑2_数据与市场.md","30_主脑3_策略仿真与研究.md","40_主脑4_独立审查.md"]:
        if not (root/f).is_file():errors.append("missing role prompt "+f)
    if by_id.get("T15",{}).get("optional") or by_id.get("T15",{}).get("phase")!="G2":errors.append("V4 read must be G2 core")
    seen=set()
    def ancestors(k):
        if k in seen or k not in by_id:return
        seen.add(k)
        for d in by_id[k]["depends_on"]:ancestors(d)
    ancestors("T48")
    if "T12" in seen:errors.append("offline gate blocked by live authorization")
    for f in ("signing_enabled","broadcast_enabled","real_notification_enabled"):
        if cfg.get(f) is not False:errors.append("unsafe default: "+f)
    if cfg.get("dry_run") is not True or cfg.get("capital_authorization") is not None:errors.append("money authority default")
    if cfg.get("rpc_endpoints") or cfg.get("venue_registry"):errors.append("unverified live defaults")
    if cfg.get("chain_id")!=5042 or cfg.get("block_domain")!="l1":errors.append("network profile drift")
    if cfg.get("native_identifier")!="usdc":errors.append("wrong native asset")
    # Parse every JSON to catch accidental corrupt templates.
    for p in root.rglob("*.json"):
        try:json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:errors.append(f"invalid JSON {p.name}: {e}")
    ownership=json.loads((root/"ownership.seed.json").read_text(encoding="utf-8"))
    actual={}
    for t in ts:
        for p in t["planned_write_paths"]:actual.setdefault(p,[]).append(t["id"])
    if actual!=ownership.get("planned_paths"):errors.append("ownership manifest drift")
    return list(dict.fromkeys(errors))

def example_review_gate(review: dict, sha: str, scope: str) -> bool:
    """Gate specification example used only for this package's self-test."""
    return bool(review.get("verdict")=="PASS" and review.get("candidate_sha")==sha
                and review.get("scope")==scope and review.get("required_tests")
                and set(review["required_tests"])<=set(review.get("executed_tests",[]))
                and not review.get("blocking_findings"))

def example_source_diff_classification(selected_code_matches: bool | None,
                                       changes: list[dict]) -> str:
    """Illustrates the plan contract only; does not inspect a real repository or authorize any input."""
    if selected_code_matches is not True:
        return "CODE_OR_POLICY_REVIEW" if selected_code_matches is False else "UNCLASSIFIED"
    if any(c.get("kind") in {"code","policy","schema"} for c in changes):
        return "CODE_OR_POLICY_REVIEW"
    if any(c.get("kind") not in {"pool_catalog","irrelevant_runtime"} for c in changes):
        return "UNCLASSIFIED"
    if any(c.get("kind")=="pool_catalog" and
           (c.get("content_validated") is not True or c.get("snapshot_stable") is not True)
           for c in changes):
        return "DATA_SNAPSHOT_UNAVAILABLE"
    return "DATA_ONLY_DIFFERENCE" if changes else "MATCH"


def self_tests(root: Path) -> dict:
    m=json.loads((root/"task-manifest.json").read_text(encoding="utf-8"))
    cfg=json.loads((root/"templates/arc-network.example.json").read_text(encoding="utf-8"))
    cases=[]
    def record(name, passed):cases.append({"name":name,"passed":bool(passed)})
    record("normal package",not issues(root,m,cfg))
    bad=copy.deepcopy(m);bad["tasks"][0]["depends_on"]=["T04"]
    record("reject cyclic dependency",any("cycle" in e for e in issues(root,bad,cfg)))
    bad=copy.deepcopy(m);bad["tasks"][6]["planned_write_paths"].append("arbitrage_contracts/arc_extensions.py")
    record("reject cross-role file claim",any("overlap" in e for e in issues(root,bad,cfg)))
    bad=copy.deepcopy(cfg);bad["broadcast_enabled"]=True
    record("reject broadcast enabled",any("unsafe default" in e for e in issues(root,m,bad)))
    bad=copy.deepcopy(m);bad["upstream_sha"]="f"*40
    record("reject source version drift",any("sha drift" in e for e in issues(root,bad,cfg)))
    bad=copy.deepcopy(m);bad["main_brains"].append("M5")
    record("reject fifth main brain",any("four roles" in e for e in issues(root,bad,cfg)))
    bad=copy.deepcopy(m);bad["tasks"][-1]["depends_on"].append("T12")
    record("separate offline and live gate",any("live authorization" in e for e in issues(root,bad,cfg)))
    r={"verdict":"PASS","candidate_sha":"abc","scope":"G1","required_tests":["e2e"],"executed_tests":["e2e"],"blocking_findings":[]}
    record("accept matching scoped PASS",example_review_gate(r,"abc","G1"))
    r["verdict"]="FAIL";r["report_delivered"]=True
    record("reject delivered FAIL as gate",not example_review_gate(r,"abc","G1"))
    r["verdict"]="PASS"
    record("reject stale review candidate",not example_review_gate(r,"new","G1"))
    r["executed_tests"]=[]
    record("reject missing required evidence",not example_review_gate(r,"abc","G1"))
    change={"kind":"pool_catalog","content_validated":True,"snapshot_stable":True}
    record("allow added V3/V4 data with unchanged selected code",
           example_source_diff_classification(True,[change])=="DATA_ONLY_DIFFERENCE")
    record("code change cannot masquerade as catalog update",
           example_source_diff_classification(False,[change])=="CODE_OR_POLICY_REVIEW")
    record("mixed catalog and policy edits need review",
           example_source_diff_classification(True,[change,{"kind":"policy"}])=="CODE_OR_POLICY_REVIEW")
    record("unknown file is not a blanket data exemption",
           example_source_diff_classification(True,[{"kind":"unknown"}])=="UNCLASSIFIED")
    dirty=copy.deepcopy(change);dirty["snapshot_stable"]=False
    record("half written catalog blocks only its data snapshot",
           example_source_diff_classification(True,[dirty])=="DATA_SNAPSHOT_UNAVAILABLE")
    bad=copy.deepcopy(m);bad["source_policy"]["whole_repository_clean_required"]=True
    record("reject whole repository clean requirement",
           any("source/data policy" in e for e in issues(root,bad,cfg)))
    bad=copy.deepcopy(m);bad["source_policy"]["robinhood_catalog_to_arc_production"]=True
    record("reject cross-chain production catalog copying",
           any("source/data policy" in e for e in issues(root,bad,cfg)))
    return {"scope":"planning package static checks and example gate predicates only",
            "project_tests_run":False,"live_rpc_accessed":False,"cases":cases,
            "passed":sum(c["passed"] for c in cases),"failed":sum(not c["passed"] for c in cases)}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args();errors=issues(args.root)
    out={"planning_errors":errors}
    if args.self_test:out["self_tests"]=self_tests(args.root)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 1 if errors or out.get("self_tests",{}).get("failed",0) else 0
if __name__=="__main__":raise SystemExit(main())
