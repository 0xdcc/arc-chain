"""Frozen Robinhood research fixtures from 13817f4; not Arc/live pool metadata."""
from research.market_data.multicall import PoolSpec

MONITOR_POOLS: list[PoolSpec] = [
    # --- USDG / WETH (全链最主流力池, 费率极低) ---
    PoolSpec(
        address="0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca",
        label="USDG/WETH 0.01%",
        fee_bps=1.0,
        tvl_usd=29_296_164,
    ),
    PoolSpec(
        address="0x69bfaf19c9f377bb306a89aed9f6b07e2c1a8d9a",
        label="USDG/WETH 0.05%",
        fee_bps=5.0,
        tvl_usd=6_258_000,
    ),
    PoolSpec(
        address="0xa9188730fe85be88ad499d7d52b099e800fb0334",
        label="USDG/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=1_900_574,
    ),
    # --- PONS / WETH ---
    PoolSpec(
        address="0x10cc6bd38112cac182db90b6a71d8bb5939526ba",
        label="PONS/WETH 1%",
        fee_bps=100.0,
        tvl_usd=8_044_963,
    ),
    PoolSpec(
        address="0xed50bdeea8adc232f159486192a4157281d722ff",
        label="PONS/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=4_957_651,
    ),
    # --- CASHCAT / WETH ---
    PoolSpec(
        address="0xa70fc67c9f69da90b63a0e4c05d229954574e313",
        label="CASHCAT/WETH 1%",
        fee_bps=100.0,
        tvl_usd=5_049_218,
    ),
    PoolSpec(
        address="0xd42a491087a15e5afd51feb3606066cc152d2b09",
        label="CASHCAT/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=3_296_389,
    ),
    # --- GLD / USDG (美股代币, 金价锚定) ---
    PoolSpec(
        address="0x7a6a053eccf1446a2633e05aa6d40d09381997ec",
        label="GLD/USDG 0.3%",
        fee_bps=30.0,
        tvl_usd=4_495_375,
    ),
    PoolSpec(
        address="0xba2f1ed4ceb2169d538d1e614d847e83c5a55913",
        label="GLD/USDG 0.05%",
        fee_bps=5.0,
        tvl_usd=707_625,
    ),
    # --- SGOV / USDG (短债 ETF 代币) ---
    PoolSpec(
        address="0xfab520051f96f4d2a32c22b6a3dd7fffdf231bfe",
        label="SGOV/USDG 0.3%",
        fee_bps=30.0,
        tvl_usd=3_570_012,
    ),
    PoolSpec(
        address="0x6ba50150b17ffd0972915aaf04ffd5e8f4fa49b4",
        label="SGOV/USDG 0.05%",
        fee_bps=5.0,
        tvl_usd=288_933,
    ),
    # --- GME / USDG (meme 股, 波动大) ---
    PoolSpec(
        address="0xe9713f453adb9245b19559790c96f470a18f2fdf",
        label="GME/USDG 1%",
        fee_bps=100.0,
        tvl_usd=1_525_089,
    ),
    PoolSpec(
        address="0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
        label="GME/USDG 0.05%",
        fee_bps=5.0,
        tvl_usd=722_081,
    ),
    # --- TSLA / USDG ---
    PoolSpec(
        address="0xf4acdaeeb7022862a763c9b1b885e11191c889e3",
        label="TSLA/USDG 0.3%",
        fee_bps=30.0,
        tvl_usd=1_422_673,
    ),
    PoolSpec(
        address="0xc4f0172d6ac8dd294dd1137d047d5e1893760236",
        label="TSLA/USDG 0.05%",
        fee_bps=5.0,
        tvl_usd=107_420,
    ),
    # --- NVDA / WETH ---
    PoolSpec(
        address="0x62ab521f71431f78ac374cdbadc6cda3c8916b6c",
        label="NVDA/WETH 0.05%",
        fee_bps=5.0,
        tvl_usd=1_120_824,
    ),
    PoolSpec(
        address="0xc0be1cb0f674d9737c72b2a63fc542361185b807",
        label="NVDA/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=299_657,
    ),
    # --- AI / WETH ---
    PoolSpec(
        address="0xc4a21f9d6485fc5893dd4a491b320a83daf4da1d",
        label="AI/WETH 1%",
        fee_bps=100.0,
        tvl_usd=2_935_901,
    ),
    PoolSpec(
        address="0xd78480cafef722d75519e13b9f516e5704d0d659",
        label="AI/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=579_376,
    ),
    # --- SLV / USDG (白银 ETF 代币) ---
    PoolSpec(
        address="0x8cb787e6c315d464775289bad00fdd67d53ecb3d",
        label="SLV/USDG 0.3%",
        fee_bps=30.0,
        tvl_usd=768_341,
    ),
    PoolSpec(
        address="0x37ed4621d1eb3abc9e551a888e6aaf0a41f7be8e",
        label="SLV/USDG 0.05%",
        fee_bps=5.0,
        tvl_usd=122_249,
    ),
    # --- GLD / WETH ---
    PoolSpec(
        address="0x98996e833ea35ec17c3645ca7b6dd40d188564c4",
        label="GLD/WETH 1%",
        fee_bps=100.0,
        tvl_usd=735_880,
    ),
    PoolSpec(
        address="0x26250ba84465454bc731f710e46f1f32b167d66b",
        label="GLD/WETH 0.05%",
        fee_bps=5.0,
        tvl_usd=382_174,
    ),
    # --- MEME / WETH ---
    PoolSpec(
        address="0x97bcdd384fc144899545deb749b6daf2aa52a2c5",
        label="MEME/WETH 1%",
        fee_bps=100.0,
        tvl_usd=733_451,
    ),
    PoolSpec(
        address="0xe2c12a7379706a291cadaaec1d22458be2f7239d",
        label="MEME/WETH 0.3%",
        fee_bps=30.0,
        tvl_usd=101_846,
    ),
]

def get_pools() -> list[PoolSpec]:
    """返回基准待监控池列表副本 (向后兼容)."""
    return list(MONITOR_POOLS)
