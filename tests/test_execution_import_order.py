"""Fresh package import orders must preserve utility and public executor access."""

import importlib
import sys

import pytest


@pytest.mark.parametrize("first", ["arbitrage.multicall_reader", "execution.funds"])
def test_fresh_monitor_and_funds_import_order(first):
    """Reproduce collection's imports without subprocesses or masking partial modules."""
    prefixes = ("arbitrage", "execution")

    def relevant(name):
        return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)

    saved = {name: module for name, module in sys.modules.items() if relevant(name)}
    try:
        for name in saved:
            del sys.modules[name]
        importlib.import_module(first)
        reader = importlib.import_module("arbitrage.multicall_reader")
        funds = importlib.import_module("execution.funds")
        package = importlib.import_module("execution")
        executor = importlib.import_module("execution.weth_arbitrage_executor")
        assert reader.MULTICALL2_ADDRESS.startswith("0x")
        assert funds.hex_value("0x" + "11" * 32, 32) == "0x" + "11" * 32
        for name in package.__all__:
            assert getattr(package, name) is getattr(executor, name)
        with pytest.raises(AttributeError):
            _ = package.not_a_public_executor
    finally:
        for name in list(sys.modules):
            if relevant(name):
                del sys.modules[name]
        sys.modules.update(saved)
