"""The official @pump-fun/pump-swap-sdk 2.1.0 EXECUTED by the external reviewer (round 13, node, network
disabled; source hashes in the fixture). 294 cases total: raw functions must reproduce every output and error
exactly; the checked layer is tested separately (it may refuse SDK outputs that are not executable). Scope limits
from the fixture's `limitations`."""
import json
from pathlib import Path

import pytest

from meme_trader.sniper import pumpswap as ps

FIX = Path(__file__).parent / "fixtures" / "pumpswap"
G = json.loads((FIX / "pumpswap-sdk-2.1.0-executed-goldens.json").read_text())
MAP = {"buyBaseInput": "buy_base_input", "buyQuoteInput": "buy_quote_input",
       "sellBaseInput": "sell_base_input", "sellQuoteInput": "sell_quote_input"}


def _case_call(case):
    """Build the positional and keyword arguments for a case."""
    i = case["inputs"]
    fees = ps.parse_fees(case["selectedFeesBps"])
    a = (int(i.get("base", i.get("quote"))), i["slippagePercent"], int(i["baseReserve"]),
         int(i["quoteReserve"]), fees, int(i["virtualQuoteReserves"]), i["coinCreator"])
    kw = {"fee_buckets": int(i["feeBucketsTotal"])} if case["api"].startswith("sell") else {}
    return a, kw


@pytest.mark.parametrize("case", G["cases"], ids=[c["id"] for c in G["cases"]])
def test_raw_functions_reproduce_the_executed_sdk(case):
    """Raw output/throw equals expected exactly; forwardSellCheck comparison when present."""
    a, kw = _case_call(case)
    try:
        actual = getattr(ps, "raw_" + MAP[case["api"]])(*a, **kw)
        actual_result = {"kind": "return", "output": {k: str(v) for k, v in actual.items()}}
    except ps.QuoteError as ex:
        actual_result = {"kind": "throw", "message": str(ex)}
    assert actual_result == case["expected"], f"case {case['id']} output mismatch"
    if "forwardSellCheck" in case:
        i = case["inputs"]
        ref = case["forwardSellCheck"]
        try:
            chk = ps.forward_sell_check(int(i["quote"]), i["slippagePercent"], int(i["baseReserve"]),
                                        int(i["quoteReserve"]), ps.parse_fees(case["selectedFeesBps"]),
                                        int(i["virtualQuoteReserves"]), i["coinCreator"],
                                        int(i["feeBucketsTotal"]))
            assert ref["kind"] == "return"
            assert {k: str(v) for k, v in chk["output"].items()} == ref["output"]
            assert chk["meetsTarget"] == ref["meetsTarget"]
            assert str(chk["shortfallAtoms"]) == ref["shortfallAtoms"]
        except ps.QuoteError as ex:
            assert ref == {"kind": "throw", "message": str(ex)}


def test_the_checked_layer_accepts_197_refuses_93_sdk_returns_and_all_4_throws():
    """Count checked layer outcomes: 197 accept returns, 93 refuse returns, 4 refuse throws."""
    accepts = {"buyBaseInput": 0, "buyQuoteInput": 0, "sellBaseInput": 0, "sellQuoteInput": 0}
    refuses_return = {"buyBaseInput": 0, "buyQuoteInput": 0, "sellBaseInput": 0, "sellQuoteInput": 0}
    refuses_throw = {"buyBaseInput": 0, "buyQuoteInput": 0, "sellBaseInput": 0, "sellQuoteInput": 0}
    for case in G["cases"]:
        a, kw = _case_call(case)
        sdk_kind = case["expected"]["kind"]
        try:
            getattr(ps, MAP[case["api"]])(*a, **kw)
            checked_kind = "accepts"
        except ps.QuoteError:
            checked_kind = "refuses"
        if sdk_kind == "return" and checked_kind == "accepts":
            accepts[case["api"]] += 1
        elif sdk_kind == "return" and checked_kind == "refuses":
            refuses_return[case["api"]] += 1
        elif sdk_kind == "throw":
            refuses_throw[case["api"]] += 1
    assert sum(accepts.values()) == 197, f"Expected 197 accepts, got {sum(accepts.values())}: {accepts}"
    assert sum(refuses_return.values()) == 93, f"Expected 93 refusals of SDK returns: {refuses_return}"
    assert sum(refuses_throw.values()) == 4, f"Expected 4 refusals of SDK throws: {refuses_throw}"
    assert accepts == {"buyBaseInput": 73, "buyQuoteInput": 46, "sellBaseInput": 50, "sellQuoteInput": 28}, \
        f"accepts per-API mismatch: {accepts}"
    assert refuses_return == {"buyBaseInput": 0, "buyQuoteInput": 27, "sellBaseInput": 21, "sellQuoteInput": 45}, \
        f"refuses_return per-API mismatch: {refuses_return}"
    assert refuses_throw == {"buyBaseInput": 0, "buyQuoteInput": 0, "sellBaseInput": 3, "sellQuoteInput": 1}, \
        f"refuses_throw per-API mismatch: {refuses_throw}"


def test_fixture_provenance():
    """Verify SDK version, network, case count, counts match, and source hash for sell.ts."""
    assert G["sdkVersion"] == "2.1.0"
    assert G["network"] == "disabled"
    assert len(G["cases"]) == 294
    assert G["counts"]["cases"] == 294
    assert G["counts"]["returns"] == 290
    assert G["counts"]["throws"] == 4
    assert G["sourceSha256"]["src/sdk/sell.ts"] == "97bf00f0f7b4d1d694a0143f603b8f103bdcc0943a86cfdc75492eb52ca8d94b"


def test_sweep_pairs_produce_identical_outputs():
    """For each sweep pair (before/after), the local raw functions produce identical outputs."""
    case_map = {c["id"]: c for c in G["cases"]}
    for sweep in G["sweepChecks"]:
        before_case = case_map[sweep["before"]]
        after_case = case_map[sweep["after"]]
        api = sweep["api"]
        a_before, kw_before = _case_call(before_case)
        a_after, kw_after = _case_call(after_case)
        raw_fn = getattr(ps, "raw_" + MAP[api])
        before_out = raw_fn(*a_before, **kw_before)
        after_out = raw_fn(*a_after, **kw_after)
        assert before_out == after_out, f"Sweep {sweep['before']} -> {sweep['after']} ({api}) outputs differ"
