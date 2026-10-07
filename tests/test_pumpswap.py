"""meme_trader/sniper/pumpswap.py against the official PumpSwap SDK 1.20.0's own outputs (tests/fixtures/pumpswap)."""
import base64
import json
from pathlib import Path

import pytest

from meme_trader.sniper import pumpswap as ps

FIX = Path(__file__).parent / "fixtures" / "pumpswap"
G = json.loads((FIX / "pumpswap-sdk-1.20.0-golden.json").read_text())
API = {"buyBaseInput": ps.buy_base_input, "buyQuoteInput": ps.buy_quote_input,
       "sellBaseInput": ps.sell_base_input, "sellQuoteInput": ps.sell_quote_input}


def configs(case):
    i = case["inputs"]
    g = G["globalConfig"]
    glob = {"lp": int(g["lpFeeBasisPoints"]), "protocol": int(g["protocolFeeBasisPoints"]),
            "creator": int(g["coinCreatorFeeBasisPoints"]), "creator_fee_configurable": i["creatorFeeConfigurable"]}
    if not i["feeConfigPresent"]:
        return glob, None
    fc = ps.parse_fee_config(G["feeConfig"])
    if i["feeConfigVariant"] == "exotic-unset":
        fc["exotic"] = {"lp": 0, "protocol": 0, "creator": 0}
    return glob, fc


@pytest.mark.parametrize("case", G["cases"], ids=[c["id"] for c in G["cases"]])
def test_quotes_match_the_sdk_exactly(case):
    i = case["inputs"]
    glob, fc = configs(case)
    virtual, quote_reserve, base_reserve = int(i["virtualQuoteReserves"]), int(i["quoteReserve"]), int(i["baseReserve"])
    try:
        fees = ps.fees_bps(glob, fc, i["creator"], i["baseMint"], int(i["baseMintSupply"]), base_reserve,
                           quote_reserve + virtual, i["quoteMint"], i["isMayhemMode"], int(i["creatorFeeBps"]))
        assert fees == ps.parse_fees(case["selectedFeesBps"])                   # the schedule the SDK picked
    except ps.QuoteError:                                                       # (the SDK validates reserves first)
        assert case["expected"]["kind"] == "throw"
        fees = ps.parse_fees(case["selectedFeesBps"]) if case["selectedFeesBps"] else {"lp": 0, "protocol": 0,
                                                                                         "creator": 0}
    amount = int(i["base"] if "base" in i else i["quote"])
    call = (amount, i["slippagePercent"], base_reserve, quote_reserve, fees, virtual, i["coinCreator"])
    if case["expected"]["kind"] == "throw":
        with pytest.raises(ps.QuoteError) as ex:
            API[case["api"]](*call)
        assert str(ex.value) == case["expected"]["message"]
        return
    got = API[case["api"]](*call)
    assert {k: str(v) for k, v in got.items()} == case["expected"]["output"]
    if "forwardSellCheck" in case:                                               # the inverse sell's forward check
        f, want = ps.forward_sell_check(*call), case["forwardSellCheck"]
        assert {k: str(v) for k, v in f["output"].items()} == want["output"]
        assert f["meetsTarget"] == want["meetsTarget"] and str(f["shortfallAtoms"]) == want["shortfallAtoms"]


def test_twelve_inverse_sells_fall_short_and_the_forward_check_says_so():
    short = [c for c in G["cases"] if c.get("forwardSellCheck") and not c["forwardSellCheck"]["meetsTarget"]]
    assert len(short) == G["counts"]["sdkInverseSellShortfallCases"] == 12


def test_the_canonical_pool_authority_is_the_pump_pda():
    assert ps.pool_authority(G["cases"][0]["inputs"]["baseMint"]) == G["cases"][0]["inputs"]["creator"]


@pytest.mark.parametrize("v", G["layoutVectors"], ids=[v["id"] for v in G["layoutVectors"]])
def test_pool_layout_reads_the_signed_i128(v):
    pool = ps.decode_pool(base64.b64decode(v["accountDataBase64"]))
    assert pool["virtual_quote_reserves"] == int(v["expectedVirtualQuoteReserves"])
    assert v["virtualFieldByteOffset"] == 245 and v["bytes"] == ps.POOL_LEN and pool["bytes"] == ps.POOL_LEN


MAINNET = json.loads((FIX / "mainnet-pools-2026-10-07.json").read_text())


@pytest.mark.parametrize("a", MAINNET["accounts"], ids=[a["id"] for a in MAINNET["accounts"]])
def test_real_mainnet_pools_decode_padded_with_their_signed_virtual_reserves(a):
    """Real accounts are 287-301 bytes, not the IDL's 270: the SDK fixtures alone would have refused every one."""
    p = ps.decode_pool(base64.b64decode(a["dataBase64"]))
    assert a["owner"] == ps.PUMP_AMM_PROGRAM and p["bytes"] == a["expected"]["bytes"] > ps.POOL_LEN
    assert p["virtual_quote_reserves"] == int(a["expected"]["virtual_quote_reserves"])
    assert p["undocumented_tail"] == a["expected"]["undocumented_tail"] and p["base_mint"] == a["expected"]["base_mint"]


def test_older_pools_end_at_a_field_boundary_and_truncated_ones_are_refused():
    data = base64.b64decode(G["layoutVectors"][1]["accountDataBase64"])
    old = ps.decode_pool(data[:245])                                 # before virtual_quote_reserves existed
    assert old["virtual_quote_reserves"] == 0 and old["creator_fee_bps"] == 0 and not old["is_holder_reward"]
    for bad in (data[:250], data[:242], b"\0" * 8 + data[8:]):     # mid-field, too short, wrong discriminator
        with pytest.raises(ps.QuoteError):
            ps.decode_pool(bad)
    tail = ps.decode_pool(data + b"\0" * 20 + b"\x07")
    assert tail["undocumented_tail"] and not ps.decode_pool(data + b"\0" * 31)["undocumented_tail"]


@pytest.mark.parametrize("v", G["mintDecodeVectors"], ids=["legacy", "token2022"])
def test_bare_mints_decode_and_others_are_refused(v):
    data = base64.b64decode(v["dataBase64"])
    m = ps.decode_mint(data, v["owner"])
    assert m["supply"] == int(v["expectedSupply"]) and m["decimals"] == v["expectedDecimals"]
    with pytest.raises(ps.QuoteError):
        ps.decode_mint(data, ps.DEFAULT_KEY)                                     # wrong owner
    with pytest.raises(ps.QuoteError):
        ps.decode_mint(data + b"\x01" + b"\0" * 100, ps.TOKEN_2022_PROGRAM)      # extensions: refused


def test_inputs_the_sdk_leaves_to_its_caller_are_refused():
    fees = {"lp": 20, "protocol": 5, "creator": 30}
    for bad in ((0, 1, 10 ** 12, 10 ** 9, fees), (10, 1, 10 ** 12, 10 ** 9, fees, -2 * 10 ** 9),
                (10, 100, 10 ** 12, 10 ** 9, fees), (10, 1, 10 ** 12, 10 ** 9, fees, 1 << 127)):
        with pytest.raises(ps.QuoteError):
            ps.sell_base_input(*bad)
