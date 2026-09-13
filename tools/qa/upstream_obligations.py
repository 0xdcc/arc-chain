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
import stat
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

# Legacy test single-file archive migration registry (TASK-LEGACY-V4-ARCHIVE-R1)
# Explicit single-file entry for tests/test_v4_poolkey.py under approved migration policy.
LEGACY_TEST_MIGRATION_REGISTRY: dict[str, dict[str, Any]] = {
    "tests/test_v4_poolkey.py": {
        "original_path": "tests/test_v4_poolkey.py",
        "archived_path": "docs/legacy_tests/robinhood/test_v4_poolkey.py.txt",
        "upstream_original_sha256": "1245821886735df5fabcba65ff37158c92d3ff8260d1a6b627b5ef56c27f46b7",
        "expected_archived_sha256": "96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a",
        "authorization_ticket": "TASK-V4-ARCHIVE-READINESS",
        "archive_policy": "USER_APPROVED_LEGACY_P1",
        "review_evidence": "/tmp/arc-v4-archive-readiness/RESULT.md",
        "disposition_mapping": "/tmp/arc-legacy-v4-disposition/mapping.json",
        "successor_suites": [
            "tests/arc_v3/independent/test_legacy_v4_obligations.py",
            "tests/arc_v3/independent/test_legacy_v4_metadata.py",
            "tests/arc_v3/independent/test_legacy_v4_plan_metadata.py",
            "tests/arc_v3/independent/test_v4_catalog_poolid_integrity.py",
        ],
    },
    "tests/test_rpc_policy.py": {
        "original_path": "tests/test_rpc_policy.py",
        "archived_path": "docs/legacy_tests/robinhood/test_rpc_policy.py.txt",
        "upstream_original_sha256": "20722798985cc87ad9c2b3e656fe14bba5ce66834b5307bb9f54e49366f71ed9",
        "expected_archived_sha256": "9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5",
        "authorization_ticket": "TASK-RPC-POLICY-DISPOSITION",
        "archive_policy": "USER_APPROVED_LEGACY_P1",
        "review_evidence": "/tmp/arc-rpc-policy-disposition-review/REVIEW.md",
        "disposition_mapping": "/tmp/arc-rpc-policy-disposition-review/rectified_mapping.json",
        "successor_suites": [
            "tests/arc_v3/independent/test_readonly_boundary.py",
            "tests/arc_v3/ingest/test_profile_transport.py",
            "tests/arc_v3/independent/test_http_batch_contract.py",
        ],
    },
}

# Known intentional Arc adaptations from upstream Robinhood baseline (13817f4)
KNOWN_ARC_ADAPTATIONS: dict[str, dict[str, str]] = {
    "tests/settled_cycles/test_decoders.py": {
        "reason": "QUALITY-STATIC-R1 explicit decoded-result narrowing with original assertions preserved",
        "expected_sha256": "50973fdd48c0fac8e0d9218ef63d69d690af9de4bcf97af41efa314c642a3a59",
    },
    "tests/settled_cycles/test_models.py": {
        "reason": "QUALITY-STATIC-R1 test fixture annotations and deliberate invalid-input typing boundaries",
        "expected_sha256": "657f89dc833bf95c2174a97ae8d8137a886b556b1101a8ad230d16353faccdcf",
    },
    "tests/catalog/test_import_boundary.py": {
        "reason": "CATALOG-IMPORT-FIX-R1 explicit interpreter-owned site-packages under isolated child flags",
        "expected_sha256": "2f479bef8812169d95ef49147d302c727dedfbd30f4262795d5c4e4abe161896",
    },
    "pyproject.toml": {
        "reason": "COLLECTION-R1 pytest importlib configuration for collision-free collection",
        "expected_sha256": "4e8f9d068db0e9e8ec16a9087ff712cbe617dd560e2f6c197abd2beaf97c11fe",
    },
    "AGENTS.md": {
        "reason": "Arc-Chain v3.1 architecture rules, four-mastermind governance (M1-M4), and Arc safety gates",
        "expected_sha256": "84a52af61a7ba07cadcb19cc881fa5287404e8e457bad0b9d23d65eaaf291e19",
    },
    "arbitrage_contracts/__init__.py": {
        "reason": "Arc extension contracts export (BlockDomain, NetworkProfile, OtcQuote, RawEnvelope, etc.)",
        "expected_sha256": "fa8f45b93677fa17805fa3b7c1aee0605320c4fa2b3629073a655bc284da4815",
    },
    "tests/conftest.py": {
        "reason": "Arc modular decoupling, safe config singleton handling, /tmp cache redirection harness, local audit runner subprocess whitelist (AUDIT-R3), and contracts test subprocess whitelist (CONTRACTS-AUTHORIZED-R1)",
        "expected_sha256": "4891b5dc53e3f5901e70c86ac7675701e87f360a786befcfead6a2b35e99b3db",
    },
    "arc_readiness/network.py": {
        "reason": "T07 Arc network profile separation (5042 mainnet, 5042002 testnet, L1 domain)",
        "expected_sha256": "2e6728f06d1aba0625725cf5bfa1d3c242077d37f9af0ca96fff1d9dbb683066",
    },
    "arc_readiness/rpc_readonly.py": {
        "reason": "T07 strict readonly RPC allowlist, batch safety, and 3-failure circuit breaker",
        "expected_sha256": "9e9ea80c5964c594e152fce4f81b7e4d879807e58dbd52941871647c29ab07c5",
    },
    "arc_readiness/balances.py": {
        "reason": "T10 USDC dual interface balance reconciliation, gas netting, and eligibility scoping",
        "expected_sha256": "a77797967d6e6be0cc9a0aed3c007a09a9c46d2bc227357dea5959661b5130f0",
    },
    "arc_readiness/eligibility.py": {
        "reason": "T10 USDC dual interface balance reconciliation, gas netting, and eligibility scoping",
        "expected_sha256": "f95b8ab6c6a7cfe8c97b2d8195bfdbb649718aca11e53633c1b0847a71312d48",
    },
    "arc_readiness/fees.py": {
        "reason": "T10 USDC dual interface balance reconciliation, gas netting, and eligibility scoping",
        "expected_sha256": "4a4e3f9761a50a6bee7b3d3d10758107bd0cfd94bbdb87271cc08ad3832b7da6",
    },
    "tests/contracts/test_manifest_coverage.py": {
        "reason": "RESTORE-FIX-R1 PUBLIC_FILES priority collection for docs/reuse JSON and exact staging probe verification",
        "expected_sha256": "5d813228e42503905eba49c17946807325cb3a3173eea847e6b1338b4def8609",
    },
    "tests/rwa/test_cli_e2e.py": {
        "reason": "RESTORE-FIX-R1 absolute CLI script resolution under tmp_path isolation with subprocess PYTHONPATH injection",
        "expected_sha256": "17bd280492f80f38e10d34bb4256f433ddc13c5e4336ea9150f82d97652c4602",
    },
    "tests/arc_readiness/test_network.py": {
        "reason": "T07 Arc network profile separation, 5042 mainnet default guard and testnet chain ID verification fixtures",
        "expected_sha256": "9879a0a0dd74b9daff4646bdbccdad3b4350d4e92f113b8314b59e8c748e2825",
    },
    "tests/arc_readiness/test_eligibility.py": {
        "reason": "T10 USDC dual interface eligibility, circular pair rejection, and canonical asset audit verification fixtures",
        "expected_sha256": "7cfd2397b03a95d1aa8a30c5a5d0541bc5b2fa91a496bb1aa61fe84ec9200af8",
    },
    "opportunities/report.py": {
        "reason": "OPPORTUNITIES-FIX-R1 build report from read-only ledger avoiding EROFS on read-only evidence filesystems",
        "expected_sha256": "6abf2f53b5384a76faa9f4cb7beff8b551571bf67b293d153ccde8ce0b8eaa1d",
    },
    "opportunities/store.py": {
        "reason": "OPPORTUNITIES-FIX-R1 read-only ledger mode without .lock/.checkpoint side effects, shared read lock, and fail-closed integrity",
        "expected_sha256": "90aaa98a605decc008c5054954b11ab758c13d08abb82592d10e1bed16670758",
    },
    "tests/opportunities/test_quote_adapter.py": {
        "reason": "OPPORTUNITIES-FIX-R1 eliminate deprecated sys.modules quoting stubs and bind real historical replay fixture",
        "expected_sha256": "b8490321568bb1f392829f3b828e5ae66abb05c305c6c1a5dc0487f879c3f694",
    },
    "tests/arc_readiness/test_quotes.py": {
        "reason": "QUALITY-TYPES-R2 explicit quote result None-check narrowing with original assertions preserved",
        "expected_sha256": "9003191064145529c4d62e822291c624291b251b94e45dbce9a6312c40332e0f",
    },
    "tests/contracts/test_legacy_adapter.py": {
        "reason": "QUALITY-TYPES-R2 explicit attribute narrowing and invalid-input typing boundaries with original assertions preserved",
        "expected_sha256": "b7d1d3ada71e3140e29adae2879185ac59c9cd2470ddb7dc9a9469ab07c776f1",
    },
    "tests/contracts/test_serialization.py": {
        "reason": "QUALITY-TYPES-R2 explicit bool assertion for unreachable narrowing and optional attribute narrowing with original assertions preserved",
        "expected_sha256": "b0a19c43ba784be9b821d5a0b63465a3892567b578dc5531d7bb7dc7f10a86ed",
    },
    "tests/settled_cycles/test_flows.py": {
        "reason": "QUALITY-TYPES-R2 explicit settlement flow None-guard narrowing with original assertions preserved",
        "expected_sha256": "d783591c01780ad1caa46e7bcd43bcf5c256ad574c0c7317725a8e721a609f70",
    },
    "tests/test_domain_contracts.py": {
        "reason": "QUALITY-TYPES-R2 cleanup obsolete type-ignore annotations with original domain contract assertions preserved",
        "expected_sha256": "d7685eefbc3538c160b7b08b41bdb64cc2a0111f56cc33e9fb5055c88d645108",
    },
    "tests/test_fix_cycle_and_profit_anomaly.py": {
        "reason": "QUALITY-TYPES-R2 cleanup obsolete type-ignore annotations and import ordering with original profit assertions preserved",
        "expected_sha256": "5862122e03832303aaeb37103ee7f1188cf0603eaaf3a2f541a3b6c9150a1695",
    },
    "tests/test_market_data_catalog.py": {
        "reason": "QUALITY-TYPES-R2 cleanup obsolete type-ignore annotation with original catalog assertions preserved",
        "expected_sha256": "7e460703e8de6fb79bf5fa1ee544f2f1b04383225c5c813122e3380460beff09",
    },
    "tests/test_spread_overflow_and_anomaly.py": {
        "reason": "QUALITY-TYPES-R2 cleanup obsolete method-assign type-ignore annotations with original spread anomaly assertions preserved",
        "expected_sha256": "0c7918fcf12afdbbc12b1327dcc4bcaa53d928a86ec626475b13fa43e4db6d5a",
    },
    "tests/receipt_fixture.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "7baade5f48b2ec073d0d57d19d6e58156793670e0f1b42c277c9659d1eb60027",
    },
    "tests/test_arbitrage_daemon.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "d1b859539255a43e3704555bbbdbf8f66b5a808541276e1e6a267753d6c623f1",
    },
    "tests/test_candidate_execution_integration.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "8265015f59b8eb5fed7d04ef6c07ee4d8f3d5d734b1c5a790118be4e6751b1d8",
    },
    "tests/test_candidate_fee_integration.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "7f211fbfbeb662cf5d80226f4c493b0de5bdc1c00ef6f2aef79d7a9f6ccd5195",
    },
    "tests/test_capacity_accuracy_and_tvl.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "090944af169478ebd252ffc2e451b8d88fdd02b041864841cac56eb24cdfb62c",
    },
    "tests/test_concurrent_reader.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "ea0648b37be3ccf276f3070d8486f53120383da27d813ab755dd9f5dada2c71a",
    },
    "tests/test_execution_service_reconciliation.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "ce96e96ba7d86e9883c0f7a15aa727b5b856b8d64865441834d9ddf52f5422dc",
    },
    "tests/test_feed_listener.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "0657af45551c631245482fd55384139869b9e69453c2add03a6a5126c796e42d",
    },
    "tests/test_fire_gate_and_new_dex.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "b6df1b8d54cdf858639349f335edd6cc3b95cba7721b4f84694c796705be2c85",
    },
    "tests/test_funds_coordinator.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "dd868d482b336963c60052c2bbd89bbe2054d844450e2099773fa023fb246426",
    },
    "tests/test_guard.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "1cc1e4a2eb2b3ad06a955a6cbdfef7ece975817b24bb848a174362cdc39d456b",
    },
    "tests/test_market_data_pool_reader.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "5f277cc6199a4a1b518845fd15279a43d79c1d50f20930be2eaa15645df4e1b7",
    },
    "tests/test_multi_rpc.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "937e5539c817e221a61a390bf58f2e9d81d3c509210a38193bde5158ebc0688f",
    },
    "tests/test_multicall_reader.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "b0477c73d9b28c964cefbf96817b127f315fd2b590afd9e206e1c4c980274f60",
    },
    "tests/test_planning.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "db9a04ecd5d51d308254f45d071a48a1c1c812ca75df34f45714defb629bbff1",
    },
    "tests/test_pool_fee_verification.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "107f4dd5be46d0390056b4bca2f2e7bfe2d192b904be56270bacc36d8b860ceb",
    },
    "tests/test_pool_scanner.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "d924fd2db5dac7b2b700ed43320904bd246d392bc9d29b74ddec0d94f1e1028d",
    },
    "tests/test_public_runtime_binding.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "2a4ab6cfae6f07e061d754e1a9432316b206a0be7c731b7e29248fcdbc1e6621",
    },
    "tests/test_quoting.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "ae388c091d0396c133d0d6de17b236b0a0c1e1c320c8c10f69a5d1ff01121c4a",
    },
    "tests/test_readonly_monitor_app.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "1b38ee41170e6776c789df7dc4a12a0f1445d08234cd3840d34408e5ef438e71",
    },
    "tests/test_remaining_funds.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "ea546d8d113bc5391cb89e35063c93170e34a45a11a695ccd0ef4a143d7d90cf",
    },
    "tests/test_remaining_monitor_auditor.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "db9c12b61c4c639c82ffb01d76142b9b37355577162ea7c8e083b9aa8276c210",
    },
    "tests/test_remaining_monitor_rpc.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "e219c01248080e0d18471d0425b0265baf1dc487b911574e97be485c9fc5f395",
    },
    "tests/test_remaining_protocols.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "ea40356c8ab7a707660f73b32c34c2a51739cc955f1830a2fd8f1238d46f652d",
    },
    "tests/test_reporting.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "c50c9d221e03e5b7b62a8dca25f14b2e2c7082a41fefba4417f9155850a927e9",
    },
    "tests/test_robinhood.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "9e4a4581fd1691539ca109938ebc6a99eb13747a7805a41563fe2cd264037acf",
    },
    "tests/test_round2_regressions.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "e5cd2b3d6f86e29f170dd7ce7f41f3145775026ecb70de30f0dec6e13ead130a",
    },
    "tests/test_round3_fixture_probe.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "1966ef57b33920a08d1e4dfa1e1ff8306f4a03bd4e181e4e34e3773852fb6a46",
    },
    "tests/test_round4_feedback.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "02b11e2dd7cfa09b60c579bf3ff53f10bf1d4d3e52f3a078363818d73dd4762d",
    },
    "tests/test_round5_phase_feedback.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "93e8ab6a1c39552e233a64f16feb63502b6566f29ad1c1eec214d7da7dfc8ef5",
    },
    "tests/test_round6_price_phase.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "6559c11db12d90ccb116d310aa44231287098c6ea24a94dfb3584314a4555174",
    },
    "tests/test_round6_upstream_phase.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "32eaf4308240e4d660b21198e9ecb1b99edce8c940f3cfa98007d777f181bb54",
    },
    "tests/test_rpc_policy.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved, archived to docs/legacy_tests/robinhood/test_rpc_policy.py.txt per TASK-RPC-POLICY-DISPOSITION",
        "expected_sha256": "9540b6388e965768d36cd80098ec8a48987b027aa01a8d0d395c4c8cc92572d5",
    },
    "tests/test_safety_bootstrap.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "cce82561053099a1ac59eb4302a30881628de5187c2ad0f7584a09374d1a7968",
    },
    "tests/test_spread_monitor.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "53150c05f1cdd6c111ccac7172169bb22703ce88d1e4387586e20aa23b899137",
    },
    "tests/test_strategies.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "3b5c034f1cbe9c7b287f028d064c6ebade6ac380893f1e3e96e5096219a7e21b",
    },
    "tests/test_tax_guard.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "4c8a5df88cbc841d6faf3db4bbbf3e5d0c5fc15e8aa4b85bd9770c0e4a1d75eb",
    },
    "tests/test_tick_cache.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "441afbdc0eeab90939b5358cdaade766bcf1a49b4796350d6d500510fb559893",
    },
    "tests/test_triangular_arb.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "b2cd458f898d672e9cd390eac8d2da3f30e1087f05b9fb5a458c716d243a13eb",
    },
    "tests/test_usdg_arbitrage_executor.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "06e7f31b651a3ab26a1ca51162825ef8faaf41f0319be0e7ab4225ca9d9e93d1",
    },
    "tests/test_v4_poolkey.py": {
        "reason": "LINT-REMAINING-R1 equivalent cleanup with preserved assertions, archived to docs/legacy_tests/robinhood/test_v4_poolkey.py.txt per TASK-V4-ARCHIVE-READINESS",
        "expected_sha256": "96ff8f34e4e22c771c2aa56769b3e7ccfaf314611442c7b4303005bc41a8a90a",
    },
    "tests/test_v4_reader.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "592b42e80453f1f4857870189af0e14c1bfb9a3665ad54f2cd50ea885d43fd7e",
    },
    "tests/test_weth_arbitrage_executor.py": {
        "reason": "LINT-REMAINING-R1 equivalent specification cleanup with original assertions preserved",
        "expected_sha256": "c65fd44a3c8d8d8e1b6124e3fb13278996ca68fba6309df51eb3f48d44bfe482",
    },
    "atomic_execution/encoding.py": {
        "reason": "TASK-V4-UNKNOWN-HOOK-R1 fail-closed rejection for unknown/missing V4 hooks without zero-hook fallback per C12 and T15",
        "expected_sha256": "de72d0157477c81c56899c73eeb3c70af9c1b8ee1b327e900f13efb174cdfeb6",
    },
    "tests/atomic_execution/test_encoding.py": {
        "reason": "TASK-V4-UNKNOWN-HOOK-R1 negative test coverage for unknown/missing V4 hook fail-closed rejection per C12",
        "expected_sha256": "e39b22b441d6c1f353bd231eed7703a783426bfcf7722cd3b575d6a7adee92d3",
    },
    "tests/test_backtest_matcher.py": {
        "reason": "TASK-B1-FIFO-ISOLATION-IMPL adaptation to research.fifo, original 5 tests 39 assertions preserved, appended 2 isolation and negative control tests (23 assertions)",
        "expected_sha256": "c280cb8016ea0b0bd9118d37ea9f6e4b575d8f8575fe05613a97b60ded97b3eb",
    },
    "tests/test_backtest_cleaner.py": {
        "reason": "TASK-B3-CLEANER-ISOLATION-IMPL adaptation to research.cleaner and research.fifo, original 6 tests 25 assertions preserved",
        "expected_sha256": "8f61ec71dcc13dc618ed92f6f728d51f87700578b74ac7254ad422cc1498912b",
    },
    "tests/test_backtest_engine.py": {
        "reason": "TASK-B4-ENGINE-ISOLATION-IMPL adaptation to research.backtest and research.fifo, original 6 tests 39 assertions preserved",
        "expected_sha256": "af24a278f75e97c8e046a31c813cb0e29722f82d5783606c0a088800543d9233",
    },
    "atomic_execution/inputs.py": {
        "reason": "TASK-T6-TAX-INPUT fail-closed tax & transfer_tax verification and capability check adaptation",
        "expected_sha256": "4a39cf00e71d18f374cffe0b9224c516ee4ee0c5e38cc862883c31c741ab59a6",
    },
    "tests/atomic_execution/test_inputs.py": {
        "reason": "TASK-T6-TAX-INPUT fixture adaptation for contract_restrictions tax VERIFIED_FALSE",
        "expected_sha256": "f5879a794da03e91616a4c43a68f690b1514073ac7a8b6253070d40b2b02fdb3",
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


def validate_legacy_migration_registry() -> None:
    """Validate destination uniqueness and structural integrity of legacy migration registry.

    Fails closed if duplicate destination paths or invalid schema values are detected.
    """
    seen_targets: set[str] = set()
    for orig_path, entry in LEGACY_TEST_MIGRATION_REGISTRY.items():
        target = entry.get("archived_path")
        if not target or not isinstance(target, str):
            raise ValueError(f"Invalid archived_path in registry for '{orig_path}': {target!r}")
        if target in seen_targets:
            raise ValueError(
                f"Collision detected in LEGACY_TEST_MIGRATION_REGISTRY: archived_path must be unique, duplicate '{target}'"
            )
        seen_targets.add(target)


def resolve_imported_file_path(
    rel_path: str,
    repo_root: Path | str | None = None,
    *,
    strict: bool = False,
) -> Path:
    """Resolve imported file path, routing approved archived legacy tests to controlled archive location.

    Security and Integrity Enforcement:
    - Only hardcoded registry constants are consulted (no environment variable redirection).
    - Strict target uniqueness: fail-closed ValueError if duplicate archived_path targets are configured.
    - Prevents dual-copy drift: fail-closed ValueError if both original and archived files exist.
    - Strict path confinement: archived target must reside under docs/legacy_tests/robinhood/.
    - Strict file attributes: target must be regular file, symlinks and hardlinks (st_nlink > 1) forbidden.
    - Prevents path traversal ('..') or absolute paths.
    - Unregistered paths are resolved directly to root / rel_path without exemption.
    """
    validate_legacy_migration_registry()
    root = get_repo_root(repo_root)

    if rel_path in LEGACY_TEST_MIGRATION_REGISTRY:
        entry = LEGACY_TEST_MIGRATION_REGISTRY[rel_path]
        archived_rel = entry.get("archived_path", "")

        if not isinstance(archived_rel, str) or not archived_rel:
            raise ValueError(f"Invalid archived_path in registry for '{rel_path}': {archived_rel!r}")
        if archived_rel.startswith("/") or Path(archived_rel).is_absolute():
            raise ValueError(f"Absolute path forbidden in archived_path for '{rel_path}': {archived_rel}")
        parts = Path(archived_rel).parts
        if ".." in parts:
            raise ValueError(f"Path traversal ('..') forbidden in archived_path for '{rel_path}': {archived_rel}")

        if not archived_rel.startswith("docs/legacy_tests/robinhood/") or len(parts) != 4:
            raise ValueError(
                f"Archived path must be strictly within 'docs/legacy_tests/robinhood/': {archived_rel}"
            )

        orig_file = root / rel_path
        arch_file = root / archived_rel

        # Ancestor validation: verify all directory components under root along archived path
        cur = root
        for part in parts[:-1]:
            cur = cur / part
            try:
                st = cur.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(
                    f"Archived path ancestor directory must not be a symlink, symlinks forbidden: {part} in {archived_rel}"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(
                    f"Archived path ancestor must be a regular directory, got non-directory: {part} in {archived_rel}"
                )

        # Leaf symlink detection: reject symlink even if target is broken or missing
        if arch_file.is_symlink():
            raise ValueError(f"Archived file must be a regular file, symlinks forbidden: {archived_rel}")

        # Dual-copy drift detection
        if orig_file.exists() and arch_file.exists():
            raise ValueError(
                f"Dual-copy drift detected: both original '{rel_path}' and archived '{archived_rel}' exist on disk"
            )

        # Leaf existence and integrity validation
        if arch_file.exists():
            if not arch_file.is_file():
                raise ValueError(f"Archived file must be a regular file, symlinks forbidden: {archived_rel}")
            st = arch_file.lstat()
            if st.st_nlink > 1:
                raise ValueError(f"Hard links strictly forbidden on archived file: {archived_rel}")
            if not arch_file.resolve().is_relative_to(root.resolve()):
                raise ValueError(
                    f"Archived file resolved path escapes repository root: {archived_rel}"
                )
        elif strict:
            raise FileNotFoundError(f"Registered archived file missing: {archived_rel}")

        return arch_file

    return root / rel_path


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
        try:
            file_path = resolve_imported_file_path(rel_path, root)
        except (ValueError, FileNotFoundError) as exc:
            unexpected_mismatches.append(
                {
                    "path": rel_path,
                    "expected_sha": meta.get("sha256"),
                    "actual_sha": "ERROR",
                    "error": str(exc),
                }
            )
            continue
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
