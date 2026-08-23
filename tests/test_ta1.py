"""Tests for TA1 interchange acknowledgement and priority lanes.

The TA104 tests are the ones that matter. `E` (accepted with errors) on an
unprocessable interchange is how a sender marks a batch delivered that never
arrived, and nobody finds out until reconciliation weeks later.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import ta1 as TA1
import transactions as TX
import x12


def _good():
    c = x12.ControlNumbers()
    return TX.build_270(c, "M1", "DOE", "JANE", "19700101", "30",
                        "2024-06-12").render()


# --------------------------------------------------------------------------
# the acknowledgement code
# --------------------------------------------------------------------------

def test_a_well_formed_interchange_is_accepted():
    r = TA1.build_ta1(_good())
    assert r["ack_code"] == "A" and r["note_code"] == "000"
    assert r["segment"].startswith("TA1*")


def test_garbage_is_invalid_interchange_content():
    r = TA1.build_ta1("this is not X12 at all")
    assert r["ack_code"] == "R" and r["note_code"] == "024"


def test_a_truncated_transmission_is_premature_end_of_file():
    r = TA1.build_ta1(_good()[:60])
    assert r["ack_code"] == "R" and r["note_code"] == "023"


def test_a_duplicate_control_number_is_caught():
    raw = _good()
    first = TA1.build_ta1(raw)
    again = TA1.build_ta1(raw, seen_control_numbers=[
        first["interchange_control_number"]])
    assert again["note_code"] == "025"
    assert again["ack_code"] == "R"


def test_every_structural_error_rejects_rather_than_accepting_with_errors():
    """'E' on something unprocessable is how a sender marks a batch delivered
    that never arrived. The code is DERIVED, not passed in, so a caller cannot
    choose it."""
    for payload in ("not x12", _good()[:40], _good().replace("00501", "99999")):
        assert TA1.build_ta1(payload)["ack_code"] == "R"


def test_the_acknowledgement_code_cannot_be_supplied_by_the_caller():
    import inspect
    params = inspect.signature(TA1.build_ta1).parameters
    assert "ack_code" not in params and "code" not in params


def test_an_unsupported_version_is_rejected():
    # This generator emits 00501 in ISA12, not 00401 -- an earlier version of
    # this test replaced a string that was not in the payload and therefore
    # asserted nothing about a document it had not modified.
    r = TA1.build_ta1(_good().replace("00501", "00301"))
    assert "003" in [e["code"] for e in r["all_errors"]]


def test_a_mismatched_iea_control_number_is_caught():
    raw = _good()
    broken = raw.replace("IEA*1*000000002", "IEA*1*000000999")
    r = TA1.build_ta1(broken)
    assert "001" in [e["code"] for e in r["all_errors"]]
    assert r["ack_code"] == "R"


def test_a_wrong_group_count_is_caught():
    raw = _good()
    r = TA1.build_ta1(raw.replace("IEA*1*", "IEA*7*"))
    assert "021" in [e["code"] for e in r["all_errors"]]


def test_the_segment_carries_the_original_control_number():
    raw = _good()
    r = TA1.build_ta1(raw)
    assert r["interchange_control_number"] in r["segment"]


def test_the_result_names_the_layer_it_answers_at():
    """A 999 cannot answer here: it is itself wrapped in an envelope and
    references the GS control number of what it acknowledges, so when the
    interchange does not parse that reference is not constructible."""
    r = TA1.build_ta1("garbage")
    assert "999 cannot answer" in r["layer"]


def test_a_missing_sender_id_is_caught():
    raw = _good()
    isa = raw[:106]
    broken = isa.replace(isa.split("*")[6], " " * len(isa.split("*")[6]), 1)
    r = TA1.build_ta1(broken + raw[106:])
    assert r["ack_code"] == "R"


# --------------------------------------------------------------------------
# priority lanes
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_each_lane_gets_its_reserved_share():
    lane = TA1.Lane(per_hour=1000, burst=10, reserve=0.3, clock=FakeClock())
    r = lane.report()
    assert r[TA1.INTERACTIVE]["cap"] == pytest.approx(3.0)
    assert r[TA1.BATCH]["cap"] == pytest.approx(7.0)


def test_batch_cannot_starve_interactive():
    """THE POINT. An eligibility check at a bedside must not be refused because
    an overnight reconciliation spent the bucket."""
    clock = FakeClock()
    lane = TA1.Lane(per_hour=1000, burst=10, reserve=0.3, clock=clock)
    # drain the batch lane entirely
    while lane.check(TA1.BATCH)[0]:
        pass
    # interactive still has its reserve
    ok, detail = lane.check(TA1.INTERACTIVE)
    assert ok is True and detail["borrowed"] is False


def test_batch_may_not_borrow_from_the_interactive_reserve():
    """A reserved share both lanes can spend is not a reserve -- it is a
    suggestion, and the overnight batch will spend it every night."""
    clock = FakeClock()
    lane = TA1.Lane(per_hour=1000, burst=10, reserve=0.3, clock=clock)
    while lane.check(TA1.BATCH)[0]:
        pass
    ok, detail = lane.check(TA1.BATCH)
    assert ok is False
    assert "may not borrow" in detail["why"]


def test_interactive_may_borrow_from_idle_batch_capacity():
    clock = FakeClock()
    lane = TA1.Lane(per_hour=1000, burst=10, reserve=0.3, clock=clock)
    # spend exactly the interactive reserve (cap 3.0), then ask for one more
    for _ in range(3):
        assert lane.check(TA1.INTERACTIVE)[0] is True
    ok, detail = lane.check(TA1.INTERACTIVE)
    assert ok is True and detail["borrowed"] is True
    assert detail["borrowed_from"] == TA1.BATCH


def test_both_lanes_refuse_once_everything_is_spent():
    clock = FakeClock()
    lane = TA1.Lane(per_hour=1000, burst=10, reserve=0.3, clock=clock)
    for _ in range(20):
        lane.check(TA1.INTERACTIVE)
    assert lane.check(TA1.INTERACTIVE)[0] is False
    assert lane.check(TA1.BATCH)[0] is False


def test_lanes_refill_over_time():
    clock = FakeClock()
    lane = TA1.Lane(per_hour=3600, burst=10, reserve=0.3, clock=clock)
    for _ in range(20):
        lane.check(TA1.INTERACTIVE)
    assert lane.check(TA1.INTERACTIVE)[0] is False
    clock.advance(10)
    assert lane.check(TA1.INTERACTIVE)[0] is True


def test_an_unknown_lane_is_refused():
    lane = TA1.Lane(per_hour=100, burst=10, clock=FakeClock())
    with pytest.raises(ValueError):
        lane.check("urgent-ish")
