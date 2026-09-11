# SOURCE BASELINE & LOCAL AUDIT REPORT (T01)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Project Instance ID**: `arc-chain-1789123287`
- **Fixed Source SHA**: `13817f4e027375dd59cc7a202ae641c068525f53`
- **Local Modular Path**: `/root/projects/crypto/v2-modular/dex-sniper-engine-modular`
- **Local Git HEAD**: `13817f4e027375dd59cc7a202ae641c068525f53` (Matches Fixed SHA: True)
- **Selected Code Digest**: `1e06e54567abc368812f094110614a71976ca27841c2f664e016c330ab519e9c`
- **Total Reusable Files**: 281

## 1. Local Git Status & 07-Rule Data Classification
Local `git status --porcelain`:
```text
 M data/v3_pools_live_catalog.json
 M data/v4_pools_live_catalog.json
 M tests/test_v4_pipeline_integration.py
```

### Classification:
1. `data/v3_pools_live_catalog.json`: **DATA_ONLY_DIFFERENCE** (incremental Robinhood pools added).
2. `data/v4_pools_live_catalog.json`: **DATA_ONLY_DIFFERENCE** (incremental Robinhood pools added).
3. `tests/test_v4_pipeline_integration.py`: Local assertion threshold adaptation (`assert v3_cnt >= 137`).
- **Verdict**: In accordance with `07_本地池目录变化处理规则.md`, pure pool directory changes in Robinhood do NOT block Arc project bootstrap. No reset/clean or git push is required. Robinhood pool addresses are strictly excluded from Arc production registry.

## 2. Resource & Process Audit
- Host: `dc-PC-llm`
- Load Average: 1.77, 1.40, 1.18
- Available Memory: 6.97 GB / 11.55 GB
- Free Disk: 150.99 GB / 217.97 GB (27.0% used)
- Active Writers/Services: None conflicting. Old writer audit confirmed zero rogue writers.

## 3. Security Boundary Confirmation
- No production `.env`, private keys, or keystores accessed.
- No live trading, transfer, or approve actions authorized.
- Read-only local inspection and isolated environment setup only.
