"""Tests for envelope integrity, rejection semantics, and accumulator linkage.

Most of these are about the 70% of eligibility work that is not the happy path:
which AAA code comes back, whether a rejection is actionable, and whether the
answer preserves the difference between "no" and "we don't know".
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import transactions as T
import x12
from payer import PayerCore
from x12 import ControlNumbers


@pytest.fixture
def core():
    return PayerCore()


@pytest.fixture
def control():
    return ControlNumbers()


def ask(core, control, member_id, last="", dob="", service_type="98",
        dos="2024-06-12"):
    req = T.build_270(control, member_id, last, "", dob, service_type, dos)
    parsed = T.parse_270(req)
    resp, meta = T.build_271(control, parsed, core)
    return T.to_json(T.parse_271(resp), meta), resp, meta


# ---------------------------------------------------------------------------
# Envelopes and control numbers
# ---------------------------------------------------------------------------
def test_control_numbers_match_across_all_three_levels(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    assert x12.check_envelope(ix) == []
    assert ix.first("ISA").get(13) == ix.first("IEA").get(2)
    assert ix.first("GS").get(6) == ix.first("GE").get(2)
    assert ix.first("ST").get(2) == ix.first("SE").get(2)


def test_se01_counts_segments_from_st_to_se_inclusive(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    segs = [s.tag for s in ix.segments]
    i, j = segs.index("ST"), segs.index("SE")
    assert int(ix.first("SE").get(1)) == j - i + 1


def test_tampered_interchange_control_number_is_detected(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    broken = x12.parse(ix.render().replace("~IEA*1*", "~IEA*1*9"))
    assert any("ISA13" in p for p in x12.check_envelope(broken))


def test_wrong_segment_count_is_detected(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    broken = x12.parse(ix.render().replace("SE*", "SE*99*", 1))
    assert any("SE01" in p for p in x12.check_envelope(broken))


def test_missing_envelope_raises(control):
    with pytest.raises(x12.EnvelopeError):
        x12.check_envelope(x12.parse("ST*270*0001~SE*2*0001~"))


def test_control_numbers_are_monotonic(control):
    assert control.isa() < control.isa() < control.isa()


# ---------------------------------------------------------------------------
# 270 round trip
# ---------------------------------------------------------------------------
def test_270_parses_back_to_what_was_put_in(control):
    ix = T.build_270(control, "W123456789", "SMITH", "JANE", "1956-03-12",
                     "98", "2024-06-12")
    p = T.parse_270(ix)
    assert p["member_id"] == "W123456789"
    assert p["last_name"] == "SMITH"
    assert p["dob"] == "19560312"
    assert p["service_type"] == "98"
    assert p["service_date"] == "2024-06-12"


def test_271_survives_serialisation(core, control):
    js, resp, meta = ask(core, control, "W123456789", "SMITH", "1956-03-12")
    reparsed = T.parse_271(x12.parse(resp.render()))
    js2 = T.to_json(reparsed, meta)
    assert js2["benefit_determination"] == js["benefit_determination"]
    assert js2["member_id"] == js["member_id"]


# ---------------------------------------------------------------------------
# AAA rejection semantics -- the 70%
# ---------------------------------------------------------------------------
def test_unknown_member_rejects_with_aaa_75(core, control):
    js, _r, _m = ask(core, control, "W999999999", "NOBODY", "1970-01-01")
    assert js["answered"] is False
    assert js["rejection"]["code"] == "75"
    assert "not found" in js["rejection"]["reason"].lower()


def test_dob_mismatch_rejects_with_aaa_71_not_75(core, control):
    """The distinction is the whole point. 75 means 'we have no such member,
    check the ID'. 71 means 'we have that member and your date of birth is
    wrong'. The front desk does different things with those, and a service
    that returns 'not eligible' for both sends the patient away."""
    js, _r, _m = ask(core, control, "W123456789", "SMITH", "1956-03-13")
    assert js["rejection"]["code"] == "71"


def test_every_rejection_carries_a_follow_up_action(core, control):
    for mid, dob in [("W999999999", "1970-01-01"), ("W123456789", "1956-03-13")]:
        js, _r, _m = ask(core, control, mid, "SMITH", dob)
        assert js["rejection"]["action_code"] in x12.AAA_ACTION
        assert js["what_the_front_desk_should_do"]


def test_rejected_271_contains_no_eb_segments(core, control):
    """A rejection is not a benefit answer. Emitting EB segments alongside an
    AAA would let a receiver read benefits out of a failed inquiry."""
    _js, resp, _m = ask(core, control, "W999999999", "NOBODY", "1970-01-01")
    assert resp.find("AAA")
    assert resp.find("EB") == []


# ---------------------------------------------------------------------------
# Ambiguity preservation -- the design point
# ---------------------------------------------------------------------------
def test_benefit_determination_is_never_a_boolean(core, control):
    js, _r, _m = ask(core, control, "W123456789", "SMITH", "1956-03-12")
    assert isinstance(js["benefit_determination"], str)
    assert "eligible" not in js


def test_the_five_states_are_all_distinguishable(core, control):
    states = set()
    for mid, dob, st, dos in [
            ("W123456789", "1956-03-12", "98", "2024-06-12"),   # active
            ("W123456789", "1956-03-12", "35", "2024-06-12"),   # carve-out
            ("W123456789", "1956-03-12", "AL", "2024-06-12"),   # non-covered
            ("W456789012", "1988-10-10", "98", "2024-09-15"),   # terminated
            ("W999999999", "1970-01-01", "98", "2024-06-12")]:  # rejected
        js, _r, _m = ask(core, control, mid, "", dob, st, dos)
        states.add(js["benefit_determination"])
    assert len(states) == 5, f"states collapsed: {states}"


def test_carve_out_is_unknown_not_denied(core, control):
    """The failure that matters operationally: telling a front desk 'not
    covered' for a benefit another entity administers denies care the member
    actually has."""
    js, _r, _m = ask(core, control, "W123456789", "SMITH", "1956-03-12",
                     service_type="35")
    assert js["benefit_determination"] == "unknown_contact_other_entity"
    assert js["contact"]
    assert "NOT a denial" in js["note"]


def test_unknown_benefit_uses_eb01_u(core, control):
    _js, resp, _m = ask(core, control, "W123456789", "SMITH", "1956-03-12",
                        service_type="35")
    assert any(eb.get(1) == "U" for eb in resp.find("EB"))


def test_terminated_coverage_reports_eb01_6_and_the_end_date(core, control):
    _js, resp, meta = ask(core, control, "W456789012", "NAKAMURA",
                          "1988-10-10", dos="2024-09-15")
    assert meta["eb01"] == "6"
    assert any(d.get(1) == "347" for d in resp.find("DTP"))


def test_coverage_not_yet_effective_is_distinguished_from_terminated(core, control):
    _js, _r, meta = ask(core, control, "W567890123", "ADEYEMI", "1992-02-04",
                        dos="2024-03-01")
    assert meta["eb01"] == "6"
    assert meta["reason"] == "not yet effective"


def test_every_answered_response_is_marked_estimate_only(core, control):
    js, _r, _m = ask(core, control, "W123456789", "SMITH", "1956-03-12")
    assert js["estimate_only"] is True
    assert "point-in-time" in js["estimate_caveat"]


# ---------------------------------------------------------------------------
# Accumulators -- the linkage
# ---------------------------------------------------------------------------
def test_accumulator_moves_the_next_271(core, control):
    """The property that makes this a system rather than two toys."""
    before, _r, _m = ask(core, control, "W234567890", "OKONKWO", "1980-11-22")
    d0 = before["accumulators"]["deductible_remaining"]
    core.adjudicate("C1", "W234567890", "1234567893", "2024-06-13", "48", 4200.0)
    after, _r, _m = ask(core, control, "W234567890", "OKONKWO", "1980-11-22")
    d1 = after["accumulators"]["deductible_remaining"]
    assert d1 < d0
    assert d1 == 0.0


def test_deductible_is_never_negative(core):
    for i in range(4):
        core.adjudicate(f"C{i}", "W123456789", "1234567893", "2024-06-13",
                        "48", 5000.0)
    acc = core.accumulators("W123456789")
    assert acc["deductible_remaining"] >= 0
    assert acc["oop_remaining"] >= 0


def test_out_of_pocket_maximum_caps_member_responsibility(core):
    total = 0.0
    for i in range(12):
        r = core.adjudicate(f"X{i}", "W345678901", "1234567893", "2024-06-13",
                            "48", 6000.0)
        total += r.get("patient_resp", 0.0)
    acc = core.accumulators("W345678901")
    assert total <= acc["oop_max_total"] + 0.01, (
        "member charged more than the out-of-pocket maximum")


def test_claim_on_terminated_coverage_is_denied(core):
    r = core.adjudicate("D1", "W456789012", "1234567893", "2024-09-15",
                        "98", 180.0)
    assert r["status"] == "denied"
    assert r["paid"] == 0.0


# ---------------------------------------------------------------------------
# 276 / 277
# ---------------------------------------------------------------------------
def test_277_for_a_paid_claim(core, control):
    core.adjudicate("P1", "W123456789", "1234567893", "2024-06-13", "98", 220.0)
    _resp, meta = T.build_277(control, "P1", core)
    assert meta["found"] is True
    assert meta["category"] == "F1"
    assert x12.STATUS_CATEGORY["F1"].startswith("Finalized/Payment")


def test_277_for_a_missing_claim_uses_a4_not_an_error(core, control):
    """'Not found' is a legitimate STATUS, not a transaction failure."""
    _resp, meta = T.build_277(control, "NOPE", core)
    assert meta["found"] is False
    assert meta["category"] == "A4"


def test_277_for_a_denied_claim_carries_the_reason(core, control):
    core.adjudicate("D2", "W456789012", "1234567893", "2024-09-15", "98", 180.0)
    _resp, meta = T.build_277(control, "D2", core)
    assert meta["category"] == "F2"
    assert meta["denial_reason"]


def test_276_round_trips(control):
    ix = T.build_276(control, "W123456789", "CLM1", "2024-06-13")
    assert x12.check_envelope(ix) == []
    refs = [s for s in ix.find("REF") if s.get(1) == "1K"]
    assert refs[0].get(2) == "CLM1"


# ---------------------------------------------------------------------------
# 999 implementation acknowledgement
# ---------------------------------------------------------------------------
import ack999 as A


def test_well_formed_transaction_is_accepted(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    _out, s = A.build_999(ix, control)
    assert s["ack"] == "A" and s["n_errors"] == 0


def test_invalid_code_value_is_accepted_with_errors_not_rejected(control):
    """An unrecognised service-type code is a data error in one element, not a
    reason to discard a readable transaction."""
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    bad = x12.parse(ix.render().replace("EQ*30", "EQ*ZZ"))
    _out, s = A.build_999(bad, control)
    assert s["ack"] == "E"
    assert any("Invalid code value" in e for e in s["errors"])


def test_envelope_failure_rejects_the_whole_interchange(control):
    """A control-number mismatch means the interchange cannot be trusted.
    Accepting the transactions inside a broken envelope is how a partial file
    gets processed as though it were whole."""
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    tampered = x12.parse(ix.render().replace("~IEA*1*", "~IEA*1*9"))
    _out, s = A.build_999(tampered, control)
    assert s["ack"] == "R"
    assert "envelope" in s["note"]


def test_missing_required_segment_is_rejected(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    stripped = x12.parse(ix.render().replace("EQ*30~", ""))
    _out, s = A.build_999(stripped, control)
    assert s["ack"] == "R"


def test_error_reports_carry_a_position_so_they_are_actionable(control):
    """'Your file was rejected' is useless to whoever has to fix it."""
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    bad = x12.parse(ix.render().replace("EQ*30", "EQ*ZZ"))
    errors, _ts = A.validate(bad)
    assert errors
    e = errors[0]
    assert e.position > 0
    assert e.element is not None
    assert "segment" in e.describe() and "element" in e.describe()


def test_999_is_a_valid_interchange_itself(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    out, _s = A.build_999(ix, control)
    assert x12.check_envelope(out) == []
    assert out.first("ST").get(1) == "999"
    assert out.first("GS").get(1) == "FA"      # functional acknowledgement


def test_999_round_trips(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    bad = x12.parse(ix.render().replace("EQ*30", "EQ*ZZ"))
    out, s = A.build_999(bad, control)
    parsed = A.parse_999(x12.parse(out.render()))
    assert parsed["ack"] == s["ack"]
    assert parsed["errors"]


def test_ak9_reports_counts(control):
    ix = T.build_270(control, "W1", "A", "B", "1980-01-01", "30", "2024-06-12")
    out, _s = A.build_999(ix, control)
    ak9 = out.first("AK9")
    assert ak9.get(1) == "A"
    assert ak9.get(2) == "1"          # transaction sets included


def test_syntax_and_business_failures_are_different_channels(core, control):
    """The distinction the onboarding doc leads with: a member who does not
    exist is a BUSINESS outcome (271 + AAA); a malformed transaction is a
    SYNTAX outcome (999) with no 271 at all."""
    ix = T.build_270(control, "W999999999", "NOBODY", "", "1970-01-01",
                     "98", "2024-06-12")
    _out, ack = A.build_999(ix, control)
    assert ack["ack"] == "A", "a well-formed inquiry is syntactically fine"

    parsed = T.parse_270(ix)
    resp, meta = T.build_271(control, parsed, core)
    js = T.to_json(T.parse_271(resp), meta)
    assert js["benefit_determination"] == "inquiry_rejected"
    assert js["rejection"]["code"] == "75"
