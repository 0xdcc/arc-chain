"""Integration and unit tests for Robinhood Chain executor and Universal Router calldata."""

import pytest
from web3 import Web3

from chains import get_chain_executor, list_supported_chains
from chains.arc import ARCChainExecutor
from chains.bsc import BSCChainExecutor
from chains.robinhood import CANONICAL_PERMIT2_ADDRESS, ROBINHOOD_CHAIN_ID, RobinhoodChainExecutor
from chains.solana import SolanaChainExecutor
from core.wallet_guard import ExcessiveAmountError, InvalidSlippageError, ZeroSlippageError


@pytest.fixture
def rh_executor():
    """Fixture returning a configured RobinhoodChainExecutor instance."""
    return get_chain_executor("robinhood")


def test_user_agent_header_injection(rh_executor):
    """Verify that requests.Session strictly includes the anti-bot User-Agent header."""
    ua = rh_executor.session.headers.get("User-Agent")
    assert ua == "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", (
        f"Expected mandatory User-Agent header, found {ua}"
    )
    session_manager = getattr(rh_executor.provider, "_request_session_manager", None)
    if session_manager:
        assert session_manager._explicit_session == rh_executor.session


def test_robinhood_rpc_connectivity_and_chain_id(rh_executor):
    """Verify live connectivity to Robinhood RPC and confirm chain ID 4663."""
    assert rh_executor.connect() is True
    assert rh_executor.is_connected() is True
    assert rh_executor.w3.eth.chain_id == ROBINHOOD_CHAIN_ID
    assert rh_executor.chain_id == ROBINHOOD_CHAIN_ID


def test_robinhood_web3_status(rh_executor):
    """Verify real-time metrics retrieval from the Robinhood node."""
    status = rh_executor.get_status()
    assert status["chain"] == "robinhood"
    assert status["chain_id"] == ROBINHOOD_CHAIN_ID
    assert status["connected"] is True
    assert status["user_agent_configured"] is True
    assert isinstance(status["block_number"], int)
    assert status["block_number"] > 0
    assert isinstance(status["gas_price_wei"], int)
    assert status["gas_price_wei"] > 0
    assert status["gas_price_gwei"] > 0
    assert status["permit2_address"] == Web3.to_checksum_address(CANONICAL_PERMIT2_ADDRESS)


def test_v4_swap_calldata_assembly_and_decoding(rh_executor):
    """Verify Universal Router V4 swap calldata assembly and exact decoding."""
    token_in = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC
    token_out = "0x4200000000000000000000000000000000000006"  # WETH
    amount_in = 10_000_000
    expected_out = 5_000_000
    slippage_pct = 1.5

    result = rh_executor.build_v4_swap_calldata(
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in,
        expected_amount_out=expected_out,
        slippage_pct=slippage_pct,
    )

    # 1. Calldata selector check (Universal Router execute: 0x24856bc3)
    assert result["selector"] == "0x24856bc3"
    assert result["calldata"].startswith("0x24856bc3")

    # 2. Strict slippage min_amount_out calculation
    expected_min_out = int(expected_out * (1 - slippage_pct / 100))
    assert result["amount_out_min"] == expected_min_out
    assert result["amount_out_min"] == 4_925_000
    assert result["amount_out_min"] > 0

    # 3. Decode calldata with RouterCodec to verify on-chain structure
    decoded_func, decoded_params = rh_executor.codec.decode.function_input(result["calldata"])
    assert decoded_func.fn_name == "execute"

    # Verify V4 swap action is encoded
    v4_func, v4_dict, _ = decoded_params["inputs"][0]
    assert v4_func.fn_name.startswith("V4_SWAP")
    action_func, action_dict = v4_dict["params"][0]
    assert action_func.fn_name == "SWAP_EXACT_IN_SINGLE"

    exact_params = action_dict["exact_in_single_params"]
    assert exact_params["amountIn"] == amount_in
    assert exact_params["amountOutMinimum"] == expected_min_out


def test_calldata_zero_slippage_strictly_prohibited(rh_executor):
    """Verify that attempting to assemble calldata with zero slippage is rejected."""
    token_in = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    token_out = "0x4200000000000000000000000000000000000006"

    with pytest.raises(ZeroSlippageError):
        rh_executor.build_v4_swap_calldata(
            token_in=token_in,
            token_out=token_out,
            amount_in=1000,
            expected_amount_out=1000,
            slippage_pct=0.0,
        )


def test_dry_run_swap_execution(rh_executor):
    """Verify full dry-run swap simulation reporting."""
    token_in = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    token_out = "0x4200000000000000000000000000000000000006"

    sim = rh_executor.dry_run_swap(
        token_in=token_in,
        token_out=token_out,
        amount_in=100_000,
        slippage_pct=1.0,
        is_buy=True,
        amount_usd=50.0,
        expected_amount_out=100_000,
    )

    assert sim["status"] == "SIMULATED_SUCCESS"
    assert sim["action"] == "BUY"
    assert sim["chain_id"] == ROBINHOOD_CHAIN_ID
    assert sim["amount_in"] == 100_000
    assert sim["guaranteed_min_out"] == 99_000
    assert sim["mev_protected"] is True
    assert sim["dry_run"] is True
    assert sim["calldata_selector"] == "0x24856bc3"


def test_dry_run_swap_exceeds_amount_guard(rh_executor):
    """Verify that dry-run aborts if amount_usd exceeds $500."""
    with pytest.raises(ExcessiveAmountError):
        rh_executor.dry_run_swap(
            token_in="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            token_out="0x4200000000000000000000000000000000000006",
            amount_in=100_000,
            slippage_pct=1.0,
            amount_usd=501.0,
        )


def test_chain_factory_and_stubs():
    """Verify factory instantiation across supported chains."""
    assert "robinhood" in list_supported_chains()
    assert "bsc" in list_supported_chains()
    assert "solana" in list_supported_chains()
    assert "arc" in list_supported_chains()

    rh = get_chain_executor("robinhood")
    assert isinstance(rh, RobinhoodChainExecutor)

    bsc = get_chain_executor("bsc")
    assert isinstance(bsc, BSCChainExecutor)
    assert bsc.chain_id == 56
    assert bsc.get_status()["status"] == "STUB_RESERVED"

    sol = get_chain_executor("solana")
    assert isinstance(sol, SolanaChainExecutor)
    assert sol.chain_id is None
    assert sol.get_status()["status"] == "STUB_RESERVED"

    arc = get_chain_executor("arc")
    assert isinstance(arc, ARCChainExecutor)
    assert arc.chain_id == 5040
    assert arc.get_status()["status"] == "STUB_RESERVED"

    with pytest.raises(ValueError) as exc:
        get_chain_executor("unknown_chain")
    assert "Unsupported chain" in str(exc.value)
