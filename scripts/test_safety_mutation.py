#!/usr/bin/env python3
"""Mutation testing script for USDG contract isolation and safety checks.

Runs in an isolated temporary directory to mutate USDG token access to WETH,
verifying that tests turn RED upon mutation and GREEN when correct.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CWD = Path(__file__).resolve().parent.parent


def main() -> int:
    print("=== 开始 USDG 变异隔离测试 ===")
    tmp_dir = Path(tempfile.mkdtemp(prefix="usdg_mutation_test_"))
    try:
        # Copy required modules into temp directory for isolated mutation
        for item in [
            "core",
            "chains",
            "arbitrage",
            "execution",
            "monitors",
            "tests",
            "pyproject.toml",
        ]:
            src = CWD / item
            dst = tmp_dir / item
            if src.is_dir():
                shutil.copytree(src, dst)
            elif src.is_file():
                shutil.copy2(src, dst)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(tmp_dir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        py_bin = str(CWD / "venv" / "bin" / "python")

        # 1. Verify baseline in temp directory passes (GREEN)
        print("步骤 1: 验证未变异基线 (预期: GREEN)...")
        res_green = subprocess.run(
            [
                py_bin,
                "-m",
                "pytest",
                str(tmp_dir / "tests" / "test_usdg_arbitrage_executor.py"),
                "-q",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"未变异基线 exit code: {res_green.returncode}")
        if res_green.returncode != 0:
            print("❌ 基线未能通过:", res_green.stdout, res_green.stderr)
            return 1
        print("✅ 未变异基线通过测试 (GREEN)")

        # 2. Mutate: in execution/weth_arbitrage_executor.py, deliberately point get_usdg_balance to weth_contract
        print("步骤 2: 注入变异 (将 get_usdg_balance 读取错误指向 weth_contract)...")
        executor_py = tmp_dir / "execution" / "weth_arbitrage_executor.py"
        content = executor_py.read_text(encoding="utf-8")
        assert "self.usdg_contract.functions.balanceOf" in content, (
            "Target method not found in executor"
        )
        mutated_content = content.replace(
            "self.usdg_contract.functions.balanceOf",
            "self.weth_contract.functions.balanceOf",
        )
        executor_py.write_text(mutated_content, encoding="utf-8")

        # 3. Run pytest on mutated code (MUST FAIL / RED)
        print("步骤 3: 运行变异代码测试 (预期: RED)...")
        res_red = subprocess.run(
            [
                py_bin,
                "-m",
                "pytest",
                str(tmp_dir / "tests" / "test_usdg_arbitrage_executor.py"),
                "-q",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"变异测试 exit code: {res_red.returncode}")
        if res_red.returncode == 0:
            print("❌ 严重安全漏洞: 变异代码未能使测试变红！测试缺少辨别能力！")
            return 1

        print("✅ 变异代码成功变红 (RED)！测试具有真实防伪能力。")
        print("变异失败截获信息片段:")
        for line in res_red.stdout.splitlines():
            if "FAILED" in line or "AssertionError" in line:
                print("  >", line)

        print("=== 变异测试验证全部通过 ===")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
