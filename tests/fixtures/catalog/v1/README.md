# W1-B synthetic fixture v1

This is an internal offline fixture format, not the W0 public serialization or cross-window
manifest schema. All addresses, review claims and quote support are synthetic; they are not
statements about deployed assets or executable trades.

`synthetic_valid.jsonl` contains four assets (two distinct WETH token addresses, one USDC,
and one native ETH balance domain) and one V4 pool. Each line is an explicit discovery record.
`synthetic_invalid.jsonl` wraps rejected examples in `{case, record}` objects; unwrap `record`
to exercise the loader. `synthetic_changes.jsonl` and `synthetic_quotes.jsonl` are intentionally
empty scaffolds reserved for the later lifecycle and quote cards.

`manifest.json` records exact JSONL partition hashes and the complete W1-B Python source/test
inventory. The inventory test compares disk and manifest in both directions. It does not replace
or modify W0 staging. Adding a new W1-B source/test requires an explicit inventory update.

The manifest also carries `review_manifest_text`, the exact bytes of an internal review snapshot,
and `review_trust`, its synthetic SHA256 anchor, reviewer identity, source type and domain.
Only test fixtures read both from this file. Production callers must supply a separately reviewed,
out-of-band `ReviewTrust`; a hash or reviewer claimed by untrusted discovery input is not authority.
No production trust anchor is shipped. SHA256 pins content; it does not establish a reviewer's
identity without that external trust decision.

The review snapshot binds the exact discovery bytes (`records_sha256`), record IDs, domain,
chain, inclusive block interval, half-open time interval and exact public/role/wallet subject.
Each embedded evidence item contains raw text and its SHA256, source mode, capture time,
chain and block reference. Missing/unresolved evidence preserves the review record but excludes
it from quote eligibility. Unknown tax/rebase/pause/blacklist/whitelist restrictions do not mean
false. Declared pool restrictions must also be verified false. Native uses the public `AssetRef`
native branch and its own balance domain; it is never indexed under a fabricated ERC20 address.

Example (explicit paths and synthetic review domain):

```python
import io
import json
from pathlib import Path
from market_catalog import CatalogRegistry, ReviewTrust, load_inputs

root = Path("tests/fixtures/catalog/v1")
catalog = CatalogRegistry(load_inputs(root / "synthetic_valid.jsonl"), domain="synthetic")
assert len(catalog.list_pending_reviews()) == 5
fixture = json.loads((root / "manifest.json").read_text())
catalog.apply_review_manifest(
    io.StringIO(fixture["review_manifest_text"]),
    trust=ReviewTrust(**fixture["review_trust"]),
)
pools = catalog.list_eligible_pools(chain_id=4663, block=100, at_ms=1200)
assert len(pools) == 1
```

Discovery remains queryable without a manifest. Conflicting metadata raises rather than silently
replacing identities. Exact input bytes are retained and re-parsed at registry construction so
hand-constructed `LoadedInputs` cannot swap a reviewed subject while retaining its hash. Applying
a review replaces the review snapshot atomically; ordering/rollback policy belongs to W1-D.
Identity approval is separate from quote eligibility; all simulation and atomic-execution
capabilities remain unknown. The catalog does not produce quotes, perform RPC calls or provide CLI
commands. The required C22 tests run a clean child and independently probe side-effect blockers;
this conflicts with the legacy `tests/conftest.py` subprocess ban and requires a separately
approved test runner. Do not weaken that legacy guard.
