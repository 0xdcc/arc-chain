# Arc-Chain v3.1: Residual Risks & Operational Governance (T48 / G2)

- **Plan ID**: `ARC-4B-v3.1-20260911-13817f4`
- **Candidate SHA**: `80af5970c679a96e27a92be32e3160e1d5218d6e`
- **Reviewer**: M4 (Independent Review & Quality Assurance)
- **Status**: Formally Certified Residual Risk Disclosure

---

## 1. Boundary & Permission Residual Risks

### RISK-01: Live RPC Endpoint Gating (G1_LIVE / G2_LIVE)
- **Severity**: Medium
- **Description**: All test suites and verification layers pass with 100% success under offline execution using deterministic fixtures and memory RPC proxies. Live mainnet (5042) and testnet (5042002) RPC endpoints have not been engaged.
- **Mitigation & Operational Gate**: G1_LIVE and G2_LIVE are explicitly gated under `PENDING_AUTHORIZATION`. Live connectivity must be configured with explicit read-only API credentials, proxy rotation (mihomo), and exponential backoff.

### RISK-02: Permanent Disabled Atomic Execution
- **Severity**: Low (Protective Invariant)
- **Description**: In accordance with user governance and safety protocols, `can_atomic_execute` is hardcoded to `False` within `ArcExecutionPlan`, and mutating RPC methods (`eth_sendRawTransaction`, `eth_sendTransaction`, `personal_sign`) are blocked fail-closed by `arc_readiness/rpc_readonly.py`.
- **Mitigation**: Real funds execution remains permanently blocked until Phase G3 is formally authorized and an isolated Pathfinder execution contract (<=1U test cap) is deployed.

---

## 2. Economic & Market Dynamics Residual Risks

### RISK-03: Zero Second-Leg Liquidity on Competing Blocks
- **Severity**: High (Economic Risk)
- **Description**: Historical 500-block trace analysis shows that secondary legs (particularly Uniswap V4 pools) often experience liquidity depletion within the same block when competing bots strike. Quoted profits based solely on pre-block StateView snapshots can revert on-chain.
- **Mitigation**: `SimulationEvidenceBridge` strictly decouples estimated net profit from verified output (`output_verified=False` until atomic state-diff confirms receipt). The engine must never assume positive execution profit without post-execution trace certification.

### RISK-04: EIP-1559 Base Fee Volatility Between Quote and Inclusion
- **Severity**: Medium
- **Description**: Arc gas fee is charged in 18-decimal native USDC. Sudden priority fee spikes or rapid base fee increases across consecutive blocks can turn marginal positive spreads negative.
- **Mitigation**: `apply_single_deduction_netting` enforces an output floor requiring $Principal + Gas + 1\text{ atom}$. Trades must be rejected if estimated gas exceeds 50% of gross edge.

---

## 3. Infrastructure & Concurrency Residual Risks

### RISK-05: Local POSIX Advisory Locks vs Network Filesystems
- **Severity**: Medium
- **Description**: Concurrency safety across M1-M4 and append-only ledgers relies on `fcntl.flock` advisory locks on the local filesystem. If the workspace is hosted on networked mounts (e.g. NFS, CIFS), lock semantics may fail without active lock daemons.
- **Mitigation**: Worktrees and ledger paths must reside on local NVMe/SSD partitions. Multi-machine deployment requires switching to database-backed locks.

### RISK-06: Ingest Cursor Lag Under Reorgs
- **Severity**: Low
- **Description**: Ingest recording operates with monotonic cursor progression. Deep chain reorganizations (>3 blocks) require cursor invalidation and gap recovery via `GapRecoveryEngine`.
- **Mitigation**: `FixedBlockSampler` detects block hash drift and fails closed, refusing to sample from unanchored latest blocks.
