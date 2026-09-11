#!/usr/bin/env python3
"""QA audit tool for upstream import parity, test obligations, and data vs code separation.

This tool implements verification according to:
- docs/reuse/IMPORT_MANIFEST.json
- docs/reuse/EXCLUSION_MANIFEST.json
- docs/reuse/TEST_OBLIGATION_MAP.json
- docs/reuse/LOCAL_DATA_OVERLAY.json
- docs/plan_v3/07_本地池目录变化处理规则.md
- docs/plan_v3/40_主脑4_独立审查.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# Allowed pool catalog paths for DATA_ONLY_DIFFERENCE classification (07 rule)
ALLOWED_POOL_CATALOG_PATHS = frozenset(
    {
        "data/v3_pools_live_catalog.json",
        "data/v4_pools_live_catalog.json",
    }
)

# Known intentional Arc adaptations from upstream Robinhood baseline (13817f4)
KNOWN_ARC_ADAPTATIONS: dict[str, dict[str, str]] = {
    "AGENTS.md": {
        "reason": "Arc-Chain v3.1 architecture rules, four-mastermind governance (M1-M4), and Arc safety gates",
        "expected_sha256": "84a52af61a7ba07cadcb19cc881fa5287404e8e457bad0b9d23d65eaaf291e19",
    },
    "arbitrage_contracts/__init__.py": {
        "reason": "Arc extension contracts export (BlockDomain, NetworkProfile, OtcQuote, RawEnvelope, etc.)",
        "expected_sha256": "fa8f45b93677fa17805fa3b7c1aee0605320c4fa2b3629073a655bc284da4815",
    },
    "tests/conftest.py": {
        "reason": "Arc modular decoupling, safe config singleton handling, and /tmp cache redirection harness",
        "expected_sha256": "a197cc1d7ea7733bd6b27efa62a4af0b0aa9657a910937917542637973a689f5",
    },
    "arc_readiness/network.py": {
        "reason": "T07 Arc network profile separation (5042 mainnet, 5042002 testnet, L1 domain)",
        "expected_sha256": "2e6728f06d1aba0625725cf5bfa1d3c242077d37f9af0ca96fff1d9dbb683066",
    },
    "arc_readiness/rpc_readonly.py": {
        "reason": "T07 strict readonly RPC allowlist, batch safety, and 3-failure circuit breaker",
        "expected_sha256": "9e9ea80c5964c594e152fce4f81b7e4d879807e58dbd52941871647c29ab07c5",
    },
}

# Known Robinhood chain ID and deployments that must NEVER enter Arc 5042 registry
ROBINHOOD_CHAIN_ID = 4663
ARC_MAINNET_CHAIN_ID = 5042
ARC_TESTNET_CHAIN_ID = 5042002

ROBINHOOD_FACTORY_ADDRESSES = frozenset(
    {
        "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",  # uniswap-v2 factory
        "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",  # uniswap-v3 factory
        "0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3",  # up-v3 factory
        "0xe0c4ceb92d08ca985bb70fe0a22feb121a9854a8",  # ramses-v3 factory
        "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # uniswap-v4 manager
    }
)


def get_repo_root(anchor: Path | str | None = None) -> Path:
    """Resolve repository root from given anchor or working directory."""
    if anchor is not None:
        p = Path(anchor).resolve()
        if p.is_file():
            p = p.parent
        return p
    # Search upwards from current file
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        if (parent / "docs" / "reuse" / "IMPORT_MANIFEST.json").exists():
            return parent
    return Path.cwd().resolve()


def compute_sha256(path: Path) -> str:
    """Compute hex SHA-256 digest of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def audit_imported_files(
    repo_root: Path | str | None = None,
    allow_known_adaptations: bool = True,
) -> dict[str, Any]:
    """Audit all 281 files in IMPORT_MANIFEST.json for existence, non-emptiness, and SHA parity."""
    root = get_repo_root(repo_root)
    manifest_path = root / "docs" / "reuse" / "IMPORT_MANIFEST.json"

    if not manifest_path.exists():
        return {
            "passed": False,
            "error": f"IMPORT_MANIFEST not found at {manifest_path}",
            "total_expected": 0,
            "total_found": 0,
            "missing_files": [],
            "exact_matches": 0,
            "adapted_count": 0,
            "adapted_files": [],
            "unexpected_mismatches": [],
        }

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    imported_files: dict[str, dict[str, Any]] = manifest.get("imported_files", {})
    total_expected = manifest.get("total_imported_files", len(imported_files))

    missing_files: list[str] = []
    empty_files: list[str] = []
    exact_matches: list[str] = []
    adapted_files: list[dict[str, Any]] = []
    unexpected_mismatches: list[dict[str, Any]] = []

    for rel_path, meta in imported_files.items():
        file_path = root / rel_path
        if not file_path.exists():
            missing_files.append(rel_path)
            continue

        st_size = file_path.stat().st_size
        expected_size = meta.get("size", 0)
        if st_size == 0 and expected_size > 0:
            empty_files.append(rel_path)

        actual_sha = compute_sha256(file_path)
        expected_sha = meta.get("sha256")

        if actual_sha == expected_sha:
            exact_matches.append(rel_path)
        else:
            # Check known adaptation
            if allow_known_adaptations and rel_path in KNOWN_ARC_ADAPTATIONS:
                adaptation = KNOWN_ARC_ADAPTATIONS[rel_path]
                adapted_files.append(
                    {
                        "path": rel_path,
                        "expected_upstream_sha": expected_sha,
                        "actual_sha": actual_sha,
                        "documented_adaptation_sha": adaptation.get("expected_sha256"),
                        "matches_documented": actual_sha == adaptation.get("expected_sha256"),
                        "reason": adaptation.get("reason"),
                    }
                )
            else:
                unexpected_mismatches.append(
                    {
                        "path": rel_path,
                        "expected_sha": expected_sha,
                        "actual_sha": actual_sha,
                        "size": st_size,
                    }
                )

    passed = (
        len(missing_files) == 0
        and len(empty_files) == 0
        and len(unexpected_mismatches) == 0
        and (len(exact_matches) + len(adapted_files)) == total_expected
    )

    return {
        "passed": passed,
        "plan_id": manifest.get("plan_id"),
        "fixed_source_sha": manifest.get("fixed_source_sha"),
        "total_expected": total_expected,
        "total_found": len(imported_files) - len(missing_files),
        "missing_files": missing_files,
        "empty_files": empty_files,
        "exact_matches": len(exact_matches),
        "adapted_count": len(adapted_files),
        "adapted_files": adapted_files,
        "unexpected_mismatches": unexpected_mismatches,
    }


def verify_exclusions(repo_root: Path | str | None = None) -> dict[str, Any]:
    """Verify that all excluded files in EXCLUSION_MANIFEST.json and security items are absent."""
    root = get_repo_root(repo_root)
    manifest_path = root / "docs" / "reuse" / "EXCLUSION_MANIFEST.json"

    if not manifest_path.exists():
        return {
            "passed": False,
            "error": f"EXCLUSION_MANIFEST not found at {manifest_path}",
            "rules_checked": 0,
            "violations": [],
        }

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    excluded_items: list[dict[str, str]] = manifest.get("excluded_items", [])
    violations: list[dict[str, str]] = []

    for item in excluded_items:
        rule_path = item.get("path", "")
        category = item.get("category", "")
        reason = item.get("reason", "")

        if "*" in rule_path:
            # Glob check
            matches = list(root.glob(rule_path))
            for m in matches:
                # Do not trigger on directories themselves if empty, only files
                if m.is_file():
                    violations.append(
                        {
                            "rule": rule_path,
                            "actual_path": str(m.relative_to(root)),
                            "category": category,
                            "reason": reason,
                        }
                    )
        else:
            target = root / rule_path
            if target.exists():
                violations.append(
                    {
                        "rule": rule_path,
                        "actual_path": rule_path,
                        "category": category,
                        "reason": reason,
                    }
                )

    # Security sweep: verify no secret keys or .env files anywhere in repo
    sensitive_patterns = [".env*", "*.pem", "*.key", "*keystore*"]
    for pattern in sensitive_patterns:
        for found in root.rglob(pattern):
            # Exclude virtualenvs or .git if traversed
            rel = str(found.relative_to(root))
            if any(p in rel for p in ("venv/", ".git/", ".pytest_cache/")):
                continue
            if found.is_file():
                violations.append(
                    {
                        "rule": f"SECURITY_SWEEP:{pattern}",
                        "actual_path": rel,
                        "category": "SECURITY_RED_LINE",
                        "reason": "Sensitive credential or secret file discovered in repository",
                    }
                )

    return {
        "passed": len(violations) == 0,
        "plan_id": manifest.get("plan_id"),
        "rules_checked": len(excluded_items),
        "violations": violations,
    }


def verify_test_obligations(repo_root: Path | str | None = None) -> dict[str, Any]:
    """Verify test obligation parity, modular test mappings, and identify outdated upstream artifacts."""
    root = get_repo_root(repo_root)
    map_path = root / "docs" / "reuse" / "TEST_OBLIGATION_MAP.json"
    legacy_obligations_path = root / "tools" / "checks" / "test_obligations.json"
    run_layers_path = root / "tools" / "checks" / "run_layers.py"

    if not map_path.exists():
        return {
            "passed": False,
            "error": f"TEST_OBLIGATION_MAP not found at {map_path}",
            "mappings": [],
            "outdated_artifacts": [],
        }

    with open(map_path, encoding="utf-8") as f:
        obligation_map = json.load(f)

    mappings: list[dict[str, Any]] = []
    for entry in obligation_map.get("mappings", []):
        arc_target = entry.get("arc_target_test")
        status = entry.get("status")
        target_path = root / arc_target

        # Check if target exists as a file or if it represents a modular package/directory
        exists_as_file = target_path.exists() and target_path.is_file()

        # Check modular alternatives (e.g. tests/test_arbitrage_contracts.py -> tests/contracts/)
        modular_dir = None
        if "test_arbitrage_contracts.py" in arc_target:
            modular_dir = root / "tests" / "contracts"
        elif "test_market_catalog.py" in arc_target:
            modular_dir = root / "tests" / "catalog"
        elif "test_state_graph.py" in arc_target:
            modular_dir = root / "tests" / "state_graph"
        elif "test_opportunities.py" in arc_target:
            modular_dir = root / "tests" / "opportunities"
        elif "test_settled_cycles.py" in arc_target:
            modular_dir = root / "tests" / "settled_cycles"

        has_modular_suite = modular_dir is not None and modular_dir.is_dir()
        modular_rel = (
            str(modular_dir.relative_to(root))
            if (modular_dir is not None and has_modular_suite)
            else None
        )

        mappings.append(
            {
                "upstream_test": entry.get("upstream_test"),
                "arc_target_test": arc_target,
                "obligation_type": entry.get("obligation_type"),
                "status": status,
                "target_file_exists": exists_as_file,
                "has_modular_suite": has_modular_suite,
                "modular_directory": modular_rel,
            }
        )

    # Detect known upstream defects
    outdated_artifacts: list[dict[str, str]] = []
    if run_layers_path.exists():
        content = run_layers_path.read_text(encoding="utf-8")
        if "tests/test_arbitrage_contracts.py" in content:
            outdated_artifacts.append(
                {
                    "file": "tools/checks/run_layers.py",
                    "issue": "References outdated flat test file 'tests/test_arbitrage_contracts.py' instead of modular test suite 'tests/contracts/'",
                    "severity": "UPSTREAM_DEFECT",
                }
            )
        if 'root / "venv/bin/python"' in content:
            outdated_artifacts.append(
                {
                    "file": "tools/checks/run_layers.py",
                    "issue": "Assumes venv exists in local directory (fails in worktrees where venv is at repository root)",
                    "severity": "PORTABILITY_DEFECT",
                }
            )

    if legacy_obligations_path.exists():
        content = legacy_obligations_path.read_text(encoding="utf-8")
        if "tests/test_arbitrage_contracts.py" in content:
            outdated_artifacts.append(
                {
                    "file": "tools/checks/test_obligations.json",
                    "issue": "References legacy monolithic test paths rather than modular subpackages",
                    "severity": "UPSTREAM_DEFECT",
                }
            )

    passed = len(mappings) > 0 and all(
        m["target_file_exists"] or m["has_modular_suite"] or "INDEPENDENT" in m["status"]
        for m in mappings
    )

    return {
        "passed": passed,
        "plan_id": obligation_map.get("plan_id"),
        "mappings": mappings,
        "outdated_artifacts": outdated_artifacts,
    }


def classify_data_code_separation(
    changed_files: list[str] | set[str] | dict[str, Any],
    file_contents: dict[str, str | bytes] | None = None,
    code_digest_matches_baseline: bool = True,
) -> dict[str, Any]:
    """Classify repository differences according to 07_本地池目录变化处理规则.md.

    Rules:
    1. If ONLY registered pool catalog paths change, content is valid JSON, and code digest matches:
       -> DATA_ONLY_DIFFERENCE (Fast path: no blocker, no whole-repo clean, no remote push required).
    2. If code files, core logic, permissions, models, or address arrays change:
       -> CODE_OR_POLICY_DIFFERENCE (Cannot pretend to be data-only fast path; requires formal review).
    3. If catalog JSON is corrupted, truncated, invalid JSON, or unclassified:
       -> DATA_SNAPSHOT_UNAVAILABLE or UNCLASSIFIED (Fail closed: must not default to empty table).
    4. If pool entries contain Robinhood chain_id 4663 or factory addresses attempting injection:
       -> CROSS_CHAIN_INJECTION_REJECTED.
    """
    file_contents = file_contents or {}

    if isinstance(changed_files, dict):
        paths = set(changed_files.keys())
    else:
        paths = set(changed_files)

    if not paths:
        return {
            "classification": "NO_DIFFERENCE",
            "is_blocker": False,
            "fast_path_allowed": True,
            "requires_clean": False,
            "requires_git_push": False,
            "reason": "No files modified",
        }

    # Check for corrupted/invalid JSON in catalog files
    for path in paths:
        if path in ALLOWED_POOL_CATALOG_PATHS:
            content = file_contents.get(path)
            if content is not None:
                if isinstance(content, bytes):
                    try:
                        content_str = content.decode("utf-8")
                    except UnicodeDecodeError:
                        return {
                            "classification": "DATA_SNAPSHOT_UNAVAILABLE",
                            "is_blocker": True,
                            "fast_path_allowed": False,
                            "requires_clean": False,
                            "requires_git_push": False,
                            "reason": f"Corrupted non-UTF8 encoding in catalog file: {path}",
                        }
                else:
                    content_str = content

                try:
                    parsed = json.loads(content_str)
                    if not isinstance(parsed, (dict, list)):
                        return {
                            "classification": "UNCLASSIFIED",
                            "is_blocker": True,
                            "fast_path_allowed": False,
                            "requires_clean": False,
                            "requires_git_push": False,
                            "reason": f"Catalog file {path} parsed as scalar, expected object/array",
                        }
                except (json.JSONDecodeError, ValueError) as err:
                    return {
                        "classification": "DATA_SNAPSHOT_UNAVAILABLE",
                        "is_blocker": True,
                        "fast_path_allowed": False,
                        "requires_clean": False,
                        "requires_git_push": False,
                        "reason": f"Corrupted or truncated JSON in catalog file {path}: {err}",
                    }

    # Check for cross-chain injection: 4663 pools into Arc registry
    for path in paths:
        if path in ALLOWED_POOL_CATALOG_PATHS and path in file_contents:
            content = file_contents[path]
            content_str = content.decode("utf-8") if isinstance(content, bytes) else content
            try:
                data = json.loads(content_str)
                pools = data if isinstance(data, list) else data.get("pools", [])
                for pool in pools:
                    if isinstance(pool, dict):
                        # Chain ID check
                        cid = pool.get("chain_id") or pool.get("chainId")
                        if cid == ROBINHOOD_CHAIN_ID:
                            return {
                                "classification": "CROSS_CHAIN_INJECTION_REJECTED",
                                "is_blocker": True,
                                "fast_path_allowed": False,
                                "requires_clean": False,
                                "requires_git_push": False,
                                "reason": f"Robinhood 4663 pool address cannot be injected into Arc 5042 registry: {pool.get('address') or pool.get('id')}",
                            }
                        # Factory address check
                        factory = str(pool.get("factory", "")).lower()
                        if factory in ROBINHOOD_FACTORY_ADDRESSES:
                            return {
                                "classification": "CROSS_CHAIN_INJECTION_REJECTED",
                                "is_blocker": True,
                                "fast_path_allowed": False,
                                "requires_clean": False,
                                "requires_git_push": False,
                                "reason": f"Robinhood DEX factory {factory} cannot be registered in Arc 5042",
                            }
            except Exception:
                pass

    # Separate catalog files from other files
    catalog_changes = paths.intersection(ALLOWED_POOL_CATALOG_PATHS)
    other_changes = paths - ALLOWED_POOL_CATALOG_PATHS

    # Case 1: Only allowed pool catalog changed and code digest matches
    if catalog_changes and not other_changes and code_digest_matches_baseline:
        return {
            "classification": "DATA_ONLY_DIFFERENCE",
            "is_blocker": False,
            "fast_path_allowed": True,
            "requires_clean": False,
            "requires_git_push": False,
            "catalog_files": sorted(catalog_changes),
            "reason": "Only registered pool catalog JSON changed; code digest remains identical",
        }

    # Case 2: Code files or other configuration modified
    if other_changes or not code_digest_matches_baseline:
        return {
            "classification": "CODE_OR_POLICY_DIFFERENCE",
            "is_blocker": True,
            "fast_path_allowed": False,
            "requires_clean": False,
            "requires_git_push": False,
            "changed_code_paths": sorted(other_changes),
            "reason": (
                "Core logic, models, permissions, or code digest changed; "
                "cannot pretend to be data-only fast path"
            ),
        }

    return {
        "classification": "UNCLASSIFIED",
        "is_blocker": True,
        "fast_path_allowed": False,
        "requires_clean": False,
        "requires_git_push": False,
        "reason": f"Unclassified changes in paths: {sorted(paths)}",
    }


def validate_arc_pool_registry_isolation(
    pool_entry: dict[str, Any],
    target_chain_id: int = ARC_MAINNET_CHAIN_ID,
) -> tuple[bool, str]:
    """Validate that a candidate pool entry belongs strictly to Arc and not Robinhood."""
    chain_id = pool_entry.get("chain_id") or pool_entry.get("chainId")
    if chain_id == ROBINHOOD_CHAIN_ID:
        return (
            False,
            f"Cross-chain injection detected: Robinhood chain_id {ROBINHOOD_CHAIN_ID} rejected for Arc registry",
        )

    if chain_id is not None and chain_id not in (ARC_MAINNET_CHAIN_ID, ARC_TESTNET_CHAIN_ID):
        return (
            False,
            f"Foreign chain_id {chain_id} rejected; Arc registry requires {target_chain_id}",
        )

    # Check factory/manager address
    factory = str(pool_entry.get("factory") or pool_entry.get("venue_address") or "").lower()
    if factory in ROBINHOOD_FACTORY_ADDRESSES:
        return (
            False,
            f"Robinhood DEX factory/manager {factory} cannot be registered in Arc {target_chain_id}",
        )

    return (True, "VALID")


def run_full_audit(repo_root: Path | str | None = None) -> dict[str, Any]:
    """Execute complete QA audit suite across all dimensions."""
    root = get_repo_root(repo_root)

    import_res = audit_imported_files(root)
    exclusion_res = verify_exclusions(root)
    obligation_res = verify_test_obligations(root)

    # Check sys.path isolation
    external_prod_root = Path("/root/projects/crypto/dex-sniper-engine").resolve()
    sys_path_leakage = [p for p in sys.path if Path(p).resolve() == external_prod_root]

    overall_passed = (
        import_res.get("passed", False)
        and exclusion_res.get("passed", False)
        and obligation_res.get("passed", False)
        and len(sys_path_leakage) == 0
    )

    return {
        "passed": overall_passed,
        "repo_root": str(root),
        "import_audit": import_res,
        "exclusion_audit": exclusion_res,
        "test_obligations_audit": obligation_res,
        "sys_path_isolation": {
            "passed": len(sys_path_leakage) == 0,
            "leakage": sys_path_leakage,
        },
    }


def print_audit_report(results: dict[str, Any]) -> None:
    """Print readable audit report to stdout."""
    print("=" * 72)
    print("  ARC-CHAIN INDEPENDENT QA AUDIT REPORT (T43 / G0)")
    print("=" * 72)
    print(f"Target Repository : {results.get('repo_root')}")
    print(f"Overall Status    : {'PASSED [OK]' if results.get('passed') else 'FAILED [FAIL]'}")
    print("-" * 72)

    imp = results.get("import_audit", {})
    print("1. IMPORT MANIFEST PARITY:")
    print(f"   - Expected Files : {imp.get('total_expected')}")
    print(f"   - Found Files    : {imp.get('total_found')}")
    print(f"   - Exact Matches  : {imp.get('exact_matches')}")
    print(f"   - Adapted Files  : {imp.get('adapted_count')}")
    print(f"   - Missing Files  : {len(imp.get('missing_files', []))}")
    print(f"   - Status         : {'PASS' if imp.get('passed') else 'FAIL'}")
    for ad in imp.get("adapted_files", []):
        print(f"     * Adapted: {ad['path']} -> {ad['reason']}")

    exc = results.get("exclusion_audit", {})
    print("\n2. EXCLUSION MANIFEST ENFORCEMENT:")
    print(f"   - Rules Checked  : {exc.get('rules_checked')}")
    print(f"   - Violations     : {len(exc.get('violations', []))}")
    print(f"   - Status         : {'PASS' if exc.get('passed') else 'FAIL'}")
    for v in exc.get("violations", []):
        print(f"     ! Violation: {v['actual_path']} ({v['category']})")

    obl = results.get("test_obligations_audit", {})
    print("\n3. TEST OBLIGATION PARITY & MODULAR ARCHITECTURE:")
    print(f"   - Mapped Tests   : {len(obl.get('mappings', []))}")
    print(f"   - Outdated Items : {len(obl.get('outdated_artifacts', []))}")
    print(f"   - Status         : {'PASS' if obl.get('passed') else 'FAIL'}")
    for item in obl.get("outdated_artifacts", []):
        print(f"     ! Defect: {item['file']} -> {item['issue']}")

    sp = results.get("sys_path_isolation", {})
    print("\n4. SYS.PATH ISOLATION:")
    print(f"   - Status         : {'PASS' if sp.get('passed') else 'FAIL'}")
    if sp.get("leakage"):
        print(f"     ! Leakage: {sp.get('leakage')}")

    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit upstream import obligations, test parity, and data separation."
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Execute full QA audit and print formatted report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw audit results as JSON",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="Custom repository root path",
    )
    args = parser.parse_args()

    results = run_full_audit(args.root)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_audit_report(results)

    sys.exit(0 if results.get("passed") else 1)


if __name__ == "__main__":
    main()
