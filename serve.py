"""HTTP facade over the X12 eligibility service, with throttling and a dashboard.

Closes three named gaps: "no HTTP layer -- the REST facade is a Python function
returning a dict", "no throttling enforcement", and "no transaction dashboard".

THE DESIGN POSITION
-------------------
This serves BOTH audiences, and refuses in both languages.

  POST /x12/270      raw X12 in, raw X12 271 out. This is what a clearinghouse
                     or a trading partner's EDI stack actually speaks.
  POST /eligibility  JSON in, JSON out, for a front-end that should never have
                     to know what an EB01 code is.

The JSON facade is a translation of the 271, never a replacement for it: the
raw X12 is returned alongside, because a facade that discards the source
document makes every disagreement between the two systems unresolvable. When a
provider says "your system told us the patient was covered", the answer has to
be the 271 that was actually sent.

THROTTLING SPEAKS X12
---------------------
An over-limit inquiry gets HTTP 429 *and* a well-formed 271 carrying AAA 42
("Unable to respond at current time") / R ("Resubmission allowed"). See
`src/throttle.py` for why either alone is insufficient: a trading partner's
system parses 271s and may parse nothing else, so a JSON 429 reads to them as a
timeout -- and a timeout is retryable, which is exactly the behaviour a rate
limiter exists to prevent.

Run:
  python serve.py            serve on :8088
  python serve.py --demo     exercise it, including the throttle
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import throttle as TH
import transactions as TX
import x12
from payer import PayerCore
from x12 import AAA_ACTION, AAA_REJECT, Segment

_STATE = {"core": None, "throttle": None, "log": None, "control": None}
MAX_BYTES = 500_000


class TransactionLog:
    """Per-transaction facts for the dashboard.

    Records the AAA code on every rejection, because "rejection rate" is not an
    operational number -- `67 patient not found` is the submitter sending bad
    member IDs, `42 unable to respond` is us throttling them, and `57 invalid
    date of service` is a bug in their date formatting. Three different phone
    calls. A single rate figure conflates all of them and tells an operations
    team nothing about who to ring.
    """

    def __init__(self):
        self.rows = []

    def record(self, *, partner, transaction, outcome, aaa_code=None,
               duration_ms=0.0, http_status=200):
        self.rows.append({
            "partner": partner, "transaction": transaction,
            "outcome": outcome, "aaa_code": aaa_code,
            "aaa_reason": AAA_REJECT.get(aaa_code) if aaa_code else None,
            "duration_ms": round(duration_ms, 3), "http_status": http_status,
            "seq": len(self.rows),
        })

    def summary(self):
        n = len(self.rows)
        if not n:
            return {"total": 0}
        by_outcome, by_aaa, by_partner = {}, {}, {}
        durations = []
        for r in self.rows:
            by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
            if r["aaa_code"]:
                by_aaa[r["aaa_code"]] = by_aaa.get(r["aaa_code"], 0) + 1
            by_partner[r["partner"]] = by_partner.get(r["partner"], 0) + 1
            durations.append(r["duration_ms"])
        durations.sort()

        def pct(p):
            return durations[min(len(durations) - 1, int(len(durations) * p))]

        rejected = sum(v for k, v in by_outcome.items() if k != "accepted")
        return {
            "total": n,
            "by_outcome": by_outcome,
            "rejection_rate": rejected / n,
            "by_aaa": [{"code": c, "reason": AAA_REJECT.get(c, "?"), "n": v,
                        "share": v / n}
                       for c, v in sorted(by_aaa.items(), key=lambda kv: -kv[1])],
            "by_partner": by_partner,
            "response_ms": {"p50": pct(0.50), "p95": pct(0.95),
                            "p99": pct(0.99), "max": durations[-1]},
        }


def _next_control():
    return _STATE["control"]


def throttled_271(inquiry, detail):
    """A well-formed 271 that says 'not now', in the partner's own language."""
    control = _next_control()
    segs = [
        Segment("BHT", "0022", "11", inquiry.get("trace", "TRN1"),
                (inquiry.get("service_date") or "20240612").replace("-", ""),
                "1015"),
        Segment("HL", "1", "", "20", "1"),
        Segment("NM1", "PR", "2", "DEMO HEALTH PLAN", "", "", "", "", "PI",
                "PAYERID"),
        Segment("HL", "2", "1", "21", "1"),
        Segment("HL", "3", "2", "22", "0"),
        Segment("NM1", "IL", "1", inquiry.get("last_name", ""),
                inquiry.get("first_name", ""), "", "", "", "MI",
                inquiry.get("member_id", "")),
        # AAA at the SUBSCRIBER level with 42/R. Not an EB segment: a throttled
        # inquiry has no benefit answer, and returning EB with unknown values
        # would be worse than refusing.
        Segment("AAA", "Y", "", detail["aaa_code"], detail["aaa_action"]),
    ]
    meta = {"rejected": True, "aaa_code": detail["aaa_code"],
            "aaa_reason": AAA_REJECT[detail["aaa_code"]],
            "aaa_action": AAA_ACTION[detail["aaa_action"]],
            "throttled": True,
            "retry_after_seconds": detail["retry_after_seconds"]}
    return x12.envelope(segs, "HB", "271", control), meta


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _send(self, code, payload, headers=(), content_type="application/json"):
        body = (payload if isinstance(payload, bytes)
                else json.dumps(payload, indent=2, default=str).encode())
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _partner(self):
        """Identify the trading partner.

        Header first, then the ISA06 sender ID from the interchange itself.
        Falling back to the envelope matters: a partner posting raw X12 has
        already identified themselves inside the document, and requiring a
        second out-of-band identifier is the kind of integration friction that
        gets solved by everyone sharing one credential.
        """
        return self.headers.get("X-Trading-Partner")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/dashboard":
            return self._send(200, {
                "transactions": _STATE["log"].summary(),
                "throttle": _STATE["throttle"].report(),
                "agreements": _STATE["throttle"].agreements,
            })
        if path == "/dashboard.html":
            return self._send(200, render_dashboard().encode(), (),
                              "text/html; charset=utf-8")
        return self._send(404, {"error": "no such route",
                                "routes": ["/health", "/dashboard",
                                           "/dashboard.html",
                                           "POST /x12/270",
                                           "POST /eligibility"]})

    def do_POST(self):
        t0 = time.perf_counter()
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES:
            self.rfile.read(min(n, MAX_BYTES))
            return self._send(413, {"error": "payload too large"})
        raw = self.rfile.read(n)

        if path == "/x12/270":
            return self._x12(raw, t0)
        if path == "/eligibility":
            return self._json(raw, t0)
        return self._send(404, {"error": f"no route {path}"})

    def _x12(self, raw, t0):
        try:
            interchange = x12.parse(raw.decode("utf-8", "replace"))
            # VALIDATE THE ENVELOPE BEFORE READING THE TRANSACTION. x12.parse
            # is tolerant by design and returns an Interchange for anything,
            # so without this a payload of "this is not X12" parsed to an empty
            # inquiry, reached build_271, and came back 200 with AAA 75
            # "subscriber not found" -- an answer about a member, to something
            # that was never a transaction. Found by a test that expected 400
            # and got 200.
            #
            # In X12 terms a malformed interchange is a TA1 interchange
            # acknowledgement, not a 271 at all. TA1 is not implemented here
            # (it is on the gap list), so this returns HTTP 400 and says so
            # rather than pretending the envelope was fine.
            problems = x12.check_envelope(interchange)
            if problems:
                raise x12.EnvelopeError("; ".join(problems)
                                        if isinstance(problems, list)
                                        else str(problems))
            inquiry = TX.parse_270(interchange)
            if not inquiry.get("member_id"):
                raise x12.EnvelopeError(
                    "no subscriber identifier in the interchange; this is not "
                    "a usable 270")
        except Exception as exc:                       # noqa: BLE001
            _STATE["log"].record(partner=self._partner() or "UNKNOWN",
                                 transaction="270", outcome="unparseable",
                                 duration_ms=(time.perf_counter() - t0) * 1000,
                                 http_status=400)
            return self._send(400, {
                "error": "not a usable 270",
                "detail": str(exc)[:200],
                "note": ("a real implementation answers a malformed "
                         "interchange with a TA1 interchange acknowledgement, "
                         "not an HTTP status. TA1 is not implemented here."),
            })

        partner = (self._partner() or interchange.sender_id
                   or "UNKNOWN")
        ok, detail = _STATE["throttle"].check(partner)
        if not ok:
            out, meta = throttled_271(inquiry, detail)
            _STATE["log"].record(partner=partner, transaction="270",
                                 outcome="throttled",
                                 aaa_code=meta["aaa_code"],
                                 duration_ms=(time.perf_counter() - t0) * 1000,
                                 http_status=429)
            # BOTH LANGUAGES. HTTP 429 for anything speaking HTTP, and a real
            # 271 in the body for anything speaking X12.
            return self._send(
                429, out.render().encode(),
                [("Retry-After", str(detail["retry_after_seconds"])),
                 ("X-Throttle-Policy",
                  f"{detail['per_hour']}/hour burst {detail['burst']}"),
                 ("X-AAA-Code", detail["aaa_code"])],
                "application/edi-x12")

        out, meta = TX.build_271(_next_control(), inquiry, _STATE["core"])
        _STATE["log"].record(
            partner=partner, transaction="270",
            outcome="rejected" if meta.get("rejected") else "accepted",
            aaa_code=meta.get("aaa_code"),
            duration_ms=(time.perf_counter() - t0) * 1000)
        return self._send(200, out.render().encode(), (),
                          "application/edi-x12")

    def _json(self, raw, t0):
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad JSON"})
        for f in ("member_id", "last_name"):
            if not req.get(f):
                return self._send(400, {"error": f"{f} is required"})

        partner = self._partner() or "UNKNOWN"
        ok, detail = _STATE["throttle"].check(partner)
        if not ok:
            inquiry = dict(req)
            out, meta = throttled_271(inquiry, detail)
            _STATE["log"].record(partner=partner, transaction="270",
                                 outcome="throttled", aaa_code=meta["aaa_code"],
                                 duration_ms=(time.perf_counter() - t0) * 1000,
                                 http_status=429)
            return self._send(429, {
                "answer": "cannot answer -- rate limited",
                "aaa_code": meta["aaa_code"], "aaa_reason": meta["aaa_reason"],
                "aaa_action": meta["aaa_action"],
                "retry_after_seconds": detail["retry_after_seconds"],
                "policy": f"{detail['per_hour']}/hour, burst {detail['burst']}",
                "escalation": detail["escalation"],
                "x12_271": out.render(),
            }, [("Retry-After", str(detail["retry_after_seconds"]))])

        inquiry = {"member_id": req["member_id"], "last_name": req["last_name"],
                   "first_name": req.get("first_name", ""),
                   "dob": req.get("dob"),
                   "service_type": req.get("service_type", "30"),
                   "service_date": req.get("service_date"),
                   "provider_npi": req.get("provider_npi", ""),
                   "trace": req.get("trace", "TRN1")}
        out, meta = TX.build_271(_next_control(), inquiry, _STATE["core"])
        parsed = TX.parse_271(out)
        body = TX.to_json(parsed, meta)
        # THE SOURCE DOCUMENT TRAVELS WITH THE TRANSLATION. When a provider
        # says "your system told us the patient was covered", the answer has to
        # be the 271 that was actually sent, not a re-rendering of a summary.
        body["x12_271"] = out.render()
        _STATE["log"].record(
            partner=partner, transaction="270",
            outcome="rejected" if meta.get("rejected") else "accepted",
            aaa_code=meta.get("aaa_code"),
            duration_ms=(time.perf_counter() - t0) * 1000)
        return self._send(200, body)


def render_dashboard():
    s = _STATE["log"].summary()
    th = _STATE["throttle"].report()
    if not s.get("total"):
        return "<!doctype html><meta charset=utf-8><p>No transactions yet.</p>"

    aaa_rows = "".join(
        f"<tr><td><code>{r['code']}</code></td><td>{r['reason']}</td>"
        f"<td>{r['n']}</td><td>{r['share']:.1%}</td></tr>"
        for r in s["by_aaa"])
    part_rows = "".join(
        f"<tr><td>{r['partner']}</td><td>{r['agreed_per_hour']:,}/h</td>"
        f"<td>{r['burst']}</td><td>{r['allowed']:,}</td>"
        f"<td>{r['throttled']:,}</td><td>{r['throttled_pct']:.1%}</td>"
        f"<td>{'yes' if r['has_agreement'] else '<b>NO TPA</b>'}</td></tr>"
        for r in th)
    d = s["response_ms"]
    return f"""<!doctype html><meta charset="utf-8">
<title>X12 eligibility — transaction dashboard</title>
<style>
 body{{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;
      margin:2rem auto;padding:0 1rem;color:#1c1c1e}}
 h1{{font-size:1.4rem}} h2{{font-size:1rem;margin-top:2rem;
      border-bottom:1px solid #e5e5ea;padding-bottom:.3rem}}
 table{{border-collapse:collapse;width:100%;font-size:.88rem;margin:.6rem 0}}
 th,td{{text-align:right;padding:.35rem .6rem;border-bottom:1px solid #f0f0f2}}
 th:first-child,td:first-child{{text-align:left}}
 th{{background:#fafafa;font-size:.78rem;text-transform:uppercase;
     letter-spacing:.03em;color:#6b6b70}}
 .cards{{display:flex;gap:1rem;flex-wrap:wrap}}
 .c{{flex:1 1 160px;border:1px solid #e5e5ea;border-radius:8px;padding:.7rem .9rem}}
 .v{{font-size:1.5rem;font-weight:600}} .l{{font-size:.75rem;color:#6b6b70;
     text-transform:uppercase;letter-spacing:.04em}}
 .note{{color:#6b6b70;font-size:.85rem}}
</style>
<h1>Transaction dashboard</h1>
<div class="cards">
  <div class="c"><div class="l">Transactions</div><div class="v">{s['total']:,}</div></div>
  <div class="c"><div class="l">Rejection rate</div><div class="v">{s['rejection_rate']:.1%}</div></div>
  <div class="c"><div class="l">p95 response</div><div class="v">{d['p95']:.1f} ms</div></div>
  <div class="c"><div class="l">p99 response</div><div class="v">{d['p99']:.1f} ms</div></div>
</div>

<h2>Rejections by AAA code</h2>
<table><thead><tr><th>AAA03</th><th>Reason</th><th>n</th><th>share</th></tr></thead>
<tbody>{aaa_rows}</tbody></table>
<p class="note"><strong>By code, not as one rate.</strong> <code>67</code> is
the submitter sending bad member IDs, <code>42</code> is us throttling them,
<code>57</code> is their date formatting. Three different phone calls. A single
rejection-rate figure conflates all of them and tells an operations team
nothing about who to ring.</p>

<h2>Trading partners</h2>
<table><thead><tr><th>Partner</th><th>Agreed</th><th>Burst</th><th>Allowed</th>
<th>Throttled</th><th>%</th><th>TPA on file</th></tr></thead>
<tbody>{part_rows}</tbody></table>
<p class="note">The limits are terms of a <strong>trading-partner
agreement</strong>, not engineering constants. Sustained volume above the
agreed rate is a commercial matter: serve the agreed rate, refuse the excess in
a form the partner's system understands, and escalate. Absorbing it silently
sets a de-facto limit nobody agreed to, and the next partner discovers it
too.</p>

<h2>Response time</h2>
<table><thead><tr><th>p50</th><th>p95</th><th>p99</th><th>max</th></tr></thead>
<tbody><tr><td>{d['p50']:.1f} ms</td><td>{d['p95']:.1f} ms</td>
<td>{d['p99']:.1f} ms</td><td>{d['max']:.1f} ms</td></tr></tbody></table>
<p class="note">In-process, single client, five members and two plans. This is
a floor on real latency, not a service level.</p>
"""


def serve(port=8088, core=None, throttle=None):
    _STATE["core"] = core or PayerCore()
    _STATE["throttle"] = throttle or TH.Throttle()
    _STATE["control"] = x12.ControlNumbers()
    _STATE["log"] = TransactionLog()
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    print(f"serving on http://127.0.0.1:{port}")
    print("  POST /x12/270        raw X12 in, raw 271 out")
    print("  POST /eligibility    JSON in, JSON + the source 271 out")
    print("  GET  /dashboard      /dashboard.html")
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--port", type=int, default=8088)
    a = ap.parse_args()
    if a.demo:
        import demo_http
        demo_http.main(a.port)
    else:
        serve(a.port).serve_forever()
