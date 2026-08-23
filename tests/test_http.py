"""Tests for throttling, the HTTP facade, and the dashboard.

The throttling tests check the thing a general-purpose rate limiter gets wrong:
not whether it refuses, but whether the refusal is expressed in a language the
trading partner's system can read. An HTTP 429 with a JSON body is, to an EDI
stack, indistinguishable from a timeout -- and a timeout is retryable.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from http.server import HTTPServer

import serve as SRV
import throttle as TH
import transactions as TX
import x12
from payer import PayerCore
from x12 import AAA_ACTION, AAA_REJECT


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


# --------------------------------------------------------------------------
# the bucket
# --------------------------------------------------------------------------

def _th(per_hour=3600, burst=5, clock=None):
    return TH.Throttle({"P": {"per_hour": per_hour, "burst": burst,
                              "contact": "x", "note": ""}},
                       clock=clock or FakeClock())


def test_a_partner_may_spend_its_burst():
    t = _th(burst=5)
    assert all(t.check("P")[0] for _ in range(5))


def test_the_next_request_after_the_burst_is_refused():
    t = _th(burst=3)
    for _ in range(3):
        t.check("P")
    ok, detail = t.check("P")
    assert ok is False
    assert detail["remaining"] == 0
    assert detail["retry_after_seconds"] >= 1


def test_tokens_refill_continuously_not_in_a_fixed_window():
    """A fixed hourly window lets a partner send their whole allowance in the
    last second of one hour and again in the first second of the next -- twice
    the agreed rate across two seconds, entirely within policy as written."""
    clock = FakeClock()
    t = _th(per_hour=3600, burst=2, clock=clock)   # one token per second
    assert t.check("P")[0] and t.check("P")[0]
    assert t.check("P")[0] is False
    clock.advance(1.0)
    assert t.check("P")[0] is True


def test_refill_is_capped_at_the_burst():
    clock = FakeClock()
    t = _th(per_hour=3600, burst=2, clock=clock)
    clock.advance(100_000)                          # a very long idle
    assert t.check("P")[0] and t.check("P")[0]
    assert t.check("P")[0] is False                 # not 100k tokens


def test_the_refusal_carries_the_x12_vocabulary():
    """AAA 42 / R -- 'unable to respond at current time' / 'resubmission
    allowed'. X12 already has the concept; a rate limiter that invents its own
    is speaking a language the counterparty does not."""
    t = _th(burst=1)
    t.check("P")
    _ok, detail = t.check("P")
    assert detail["aaa_code"] == "42"
    assert detail["aaa_action"] == "R"
    assert AAA_REJECT["42"] == "Unable to respond at current time"
    assert AAA_ACTION["R"] == "Resubmission allowed"


def test_partners_have_independent_buckets():
    t = TH.Throttle({"A": {"per_hour": 3600, "burst": 1, "contact": "", "note": ""},
                     "B": {"per_hour": 3600, "burst": 1, "contact": "", "note": ""}},
                    clock=FakeClock())
    assert t.check("A")[0] and t.check("B")[0]
    assert t.check("A")[0] is False
    assert t.check("B")[0] is False


def test_an_unknown_partner_gets_the_default_agreement():
    """Throttled rather than blocked outright or served without limit -- both
    of which have been somebody's outage."""
    t = TH.Throttle({}, clock=FakeClock())
    ok, detail = t.check("STRANGER")
    assert ok is True
    assert detail["per_hour"] == TH.DEFAULT_AGREEMENT["per_hour"]


def test_the_refusal_names_the_commercial_escalation():
    """Sustained overage is a commercial matter. Absorbing it silently sets a
    de-facto limit nobody agreed to."""
    t = _th(burst=1)
    t.check("P")
    _ok, detail = t.check("P")
    assert "commercial" in detail["escalation"]
    assert detail["contact"]


def test_the_report_flags_a_partner_with_no_agreement():
    t = TH.Throttle({}, clock=FakeClock())
    t.check("STRANGER")
    row = t.report()[0]
    assert row["has_agreement"] is False


# --------------------------------------------------------------------------
# the HTTP facade
# --------------------------------------------------------------------------

@pytest.fixture
def api():
    core = PayerCore()
    clock = FakeClock()
    agreements = {"BIG": {"per_hour": 360000, "burst": 500, "contact": "b",
                          "note": ""},
                  "TINY": {"per_hour": 60, "burst": 2, "contact": "t",
                           "note": ""}}
    httpd = HTTPServer(("127.0.0.1", 0), SRV.Handler)
    SRV._STATE["core"] = core
    SRV._STATE["throttle"] = TH.Throttle(agreements, clock=clock)
    SRV._STATE["log"] = SRV.TransactionLog()
    SRV._STATE["control"] = x12.ControlNumbers()
    # Reset the interchange-duplicate history and the priority lanes: they are
    # module-level, and a test that passed alone failed in the suite because a
    # previous test had already used the same ISA control numbers.
    SRV._STATE["seen_isa"] = {}
    SRV._STATE["lanes"] = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    member = core.con.execute(
        "SELECT member_id, last_name, first_name, dob FROM member "
        "ORDER BY member_id").fetchone()
    yield base, member, clock
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body, partner=None, raw=False):
    data = body if raw else json.dumps(body).encode()
    r = urllib.request.Request(base + path, data=data, method="POST")
    if partner:
        r.add_header("X-Trading-Partner", partner)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=20) as resp:
        return resp.status, resp.read()


def test_the_json_facade_answers_an_eligible_member(api):
    base, m, _c = api
    code, _h, body = _post(base, "/eligibility",
                           {"member_id": m[0], "last_name": m[1],
                            "first_name": m[2], "dob": m[3],
                            "service_date": "2024-06-12"}, "BIG")
    assert code == 200
    assert json.loads(body)["answered"] is True


def test_the_source_271_travels_with_the_translation(api):
    """A facade that discards the source document makes every disagreement
    between two systems unresolvable."""
    base, m, _c = api
    _c2, _h, body = _post(base, "/eligibility",
                          {"member_id": m[0], "last_name": m[1]}, "BIG")
    raw = json.loads(body)["x12_271"]
    assert raw.startswith("ISA") and "~SE*" in raw
    # and it round-trips through the parser
    assert TX.parse_271(x12.parse(raw))


def test_an_unknown_member_is_200_with_an_aaa_not_a_404(api):
    """The transaction succeeded; the ANSWER is a rejection. A 404 conflates
    'we could not process your inquiry' with 'this member does not exist', and
    the front desk needs the second one to know to correct the ID."""
    base, _m, _c = api
    code, _h, body = _post(base, "/eligibility",
                           {"member_id": "NOBODY", "last_name": "NOONE"}, "BIG")
    js = json.loads(body)
    assert code == 200
    assert js["answered"] is False
    assert js["rejection"]["code"] == "75"


def test_raw_x12_in_raw_x12_out(api):
    base, m, _c = api
    control = x12.ControlNumbers()
    inq = TX.build_270(control, m[0], m[1], m[2], m[3], "30", "2024-06-12")
    code, hdrs, body = _post(base, "/x12/270", inq.render().encode(),
                             "BIG", raw=True)
    assert code == 200
    assert hdrs["Content-Type"] == "application/edi-x12"
    assert body.decode().startswith("ISA")


def test_an_unparseable_270_is_a_400(api):
    base, _m, _c = api
    code, _h, _b = _post(base, "/x12/270", b"this is not X12", "BIG", raw=True)
    assert code == 400


def test_the_throttled_json_response_carries_the_aaa_and_retry_after(api):
    base, m, _c = api
    for _ in range(2):
        _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]},
              "TINY")
    code, hdrs, body = _post(base, "/eligibility",
                             {"member_id": m[0], "last_name": m[1]}, "TINY")
    js = json.loads(body)
    assert code == 429
    assert hdrs["Retry-After"]
    assert js["aaa_code"] == "42" and js["aaa_action"] == "Resubmission allowed"


def test_the_throttled_x12_response_is_a_real_271(api):
    """THE POINT OF THE FILE. An EDI stack parses 271s and may parse nothing
    else; a JSON 429 reads to it as a timeout, and a timeout is retryable."""
    base, m, _c = api
    control = x12.ControlNumbers()
    for _ in range(3):
        inq = TX.build_270(control, m[0], m[1], m[2], m[3], "30", "2024-06-12")
        code, hdrs, body = _post(base, "/x12/270", inq.render().encode(),
                                 "TINY", raw=True)
    assert code == 429
    assert hdrs["Content-Type"] == "application/edi-x12"
    assert hdrs["X-AAA-Code"] == "42"
    text = body.decode()
    assert text.startswith("ISA")
    assert "AAA*Y**42*R" in text
    # it parses as a 271, and reads as a rejection
    parsed = TX.parse_271(x12.parse(text))
    assert parsed["rejected"] is True
    assert parsed["aaa"]["code"] == "42"


def test_a_throttled_271_carries_no_eb_segments(api):
    """A throttled inquiry has no benefit answer. Returning EB with unknown
    values would be worse than refusing."""
    base, m, _c = api
    control = x12.ControlNumbers()
    for _ in range(3):
        inq = TX.build_270(control, m[0], m[1], m[2], m[3], "30", "2024-06-12")
        _code, _h, body = _post(base, "/x12/270", inq.render().encode(),
                                "TINY", raw=True)
    assert "EB*" not in body.decode()


def test_throttling_recovers_after_the_bucket_refills(api):
    base, m, clock = api
    for _ in range(2):
        _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]},
              "TINY")
    code, _h, _b = _post(base, "/eligibility",
                         {"member_id": m[0], "last_name": m[1]}, "TINY")
    assert code == 429
    clock.advance(3600)                    # an hour of refill
    code, _h, _b = _post(base, "/eligibility",
                         {"member_id": m[0], "last_name": m[1]}, "TINY")
    assert code == 200


# --------------------------------------------------------------------------
# the dashboard
# --------------------------------------------------------------------------

def test_the_dashboard_breaks_rejections_down_by_aaa_code(api):
    """`67` is the submitter sending bad member IDs, `42` is us throttling
    them. Different phone calls. A single rate figure tells operations
    neither."""
    base, m, _c = api
    _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]}, "BIG")
    _post(base, "/eligibility", {"member_id": "NOBODY", "last_name": "X"}, "BIG")
    for _ in range(3):
        _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]},
              "TINY")

    _c2, body = _get(base, "/dashboard")
    s = json.loads(body)["transactions"]
    codes = {r["code"] for r in s["by_aaa"]}
    assert "75" in codes and "42" in codes
    assert s["total"] == 5
    assert 0 < s["rejection_rate"] < 1


def test_the_dashboard_reports_response_percentiles(api):
    base, m, _c = api
    for _ in range(5):
        _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]},
              "BIG")
    _c2, body = _get(base, "/dashboard")
    d = json.loads(body)["transactions"]["response_ms"]
    assert d["p50"] <= d["p95"] <= d["p99"] <= d["max"]


def test_the_dashboard_is_empty_before_any_traffic(api):
    base, _m, _c = api
    _c2, body = _get(base, "/dashboard")
    assert json.loads(body)["transactions"]["total"] == 0


def test_the_html_dashboard_renders(api):
    base, m, _c = api
    _post(base, "/eligibility", {"member_id": m[0], "last_name": m[1]}, "BIG")
    _c2, html = _get(base, "/dashboard.html")
    text = html.decode()
    assert "Transaction dashboard" in text
    assert "Rejections by AAA code" in text
    assert "trading-partner" in text


def test_an_unknown_route_lists_the_real_ones(api):
    base, _m, _c = api
    try:
        _get(base, "/nope")
        raise AssertionError("should 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert "POST /x12/270" in json.loads(e.read())["routes"]
