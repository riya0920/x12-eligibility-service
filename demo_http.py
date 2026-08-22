"""Exercise the HTTP facade: both languages, and the throttle in both.

Run:  python serve.py --demo
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import throttle as TH
import transactions as TX
import x12
from payer import PayerCore

import serve as SRV


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


def main(port=8089):
    core = PayerCore()
    # A deliberately tiny agreement so the throttle can be shown firing in a
    # few requests rather than a thousand.
    agreements = dict(TH.TRADING_PARTNER_AGREEMENTS)
    agreements["SUBMITTERB"] = {"per_hour": 60, "burst": 3,
                                "contact": "edi@partner-b.example",
                                "note": "pilot, tightened for this demo"}
    httpd = SRV.serve(port, core, TH.Throttle(agreements))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    members = core.con.execute(
        "SELECT member_id, last_name, first_name, dob FROM member "
        "ORDER BY member_id").fetchall()
    m = members[0]

    print("\n" + "=" * 76)
    print("EXERCISING THE HTTP FACADE")
    print("=" * 76)

    # ---- the JSON facade ---------------------------------------------------
    code, _h, body = _post(base, "/eligibility", {
        "member_id": m[0], "last_name": m[1], "first_name": m[2],
        "dob": m[3], "service_type": "30", "service_date": "2024-06-12"},
        partner="SUBMITTERA")
    js = json.loads(body)
    print(f"\n  POST /eligibility -> {code}")
    print(f"    answer: {js.get('answer')}")
    print(f"    the source 271 travels with it: "
          f"{len(js.get('x12_271', ''))} bytes")
    print("    A facade that discards the source document makes every")
    print("    disagreement between two systems unresolvable. When a provider")
    print("    says 'your system told us the patient was covered', the answer")
    print("    has to be the 271 that was actually sent.")

    # ---- raw X12 -----------------------------------------------------------
    control = x12.ControlNumbers()
    inq = TX.build_270(control, m[0], m[1], m[2], m[3], "30", "2024-06-12")
    code, hdrs, body = _post(base, "/x12/270", inq.render().encode(),
                             partner="SUBMITTERA", raw=True)
    print(f"\n  POST /x12/270 -> {code} {hdrs.get('Content-Type')}")
    print(f"    {body.decode()[:88]}...")

    # ---- the throttle, in both languages -----------------------------------
    print("\n" + "-" * 76)
    print("THROTTLING: SUBMITTERB, agreed 60/hour, burst 3")
    print("-" * 76)
    for i in range(6):
        code, hdrs, body = _post(base, "/eligibility", {
            "member_id": m[0], "last_name": m[1]}, partner="SUBMITTERB")
        if code == 200:
            print(f"    request {i + 1}: {code} ok")
        else:
            js = json.loads(body)
            print(f"    request {i + 1}: {code}  "
                  f"Retry-After {hdrs.get('Retry-After')}s  "
                  f"AAA {js['aaa_code']} '{js['aaa_reason']}' "
                  f"/ {js['aaa_action']}")

    inq = TX.build_270(control, m[0], m[1], m[2], m[3], "30", "2024-06-12")
    code, hdrs, body = _post(base, "/x12/270", inq.render().encode(),
                             partner="SUBMITTERB", raw=True)
    print(f"\n  the same refusal to an X12 client -> {code} "
          f"{hdrs.get('Content-Type')}")
    print(f"    X-AAA-Code: {hdrs.get('X-AAA-Code')}   "
          f"Retry-After: {hdrs.get('Retry-After')}")
    aaa_line = [l for l in body.decode().split("~") if l.startswith("AAA")]
    print(f"    and in the body: {aaa_line[0] if aaa_line else '(none)'}")
    print("\n    THIS IS THE POINT. A trading partner's system parses 271s and")
    print("    may parse nothing else. An HTTP 429 with a JSON body reads to")
    print("    them as a timeout -- and a timeout is retryable, which is")
    print("    exactly the behaviour a rate limiter exists to prevent. AAA")
    print("    42/R says 'not now, resubmission allowed' in their own")
    print("    language, and their software already knows how to read it.")

    # ---- an unknown partner ------------------------------------------------
    code, _h, _b = _post(base, "/eligibility",
                         {"member_id": m[0], "last_name": m[1]},
                         partner="WHOAREYOU")
    print(f"\n  an unknown submitter -> {code} (served, under the default "
          f"{TH.DEFAULT_AGREEMENT['per_hour']}/hour)")
    print("    A default exists so an unknown submitter is throttled rather")
    print("    than either blocked outright or served without limit -- both")
    print("    of which have been somebody's outage.")

    # ---- rejections that are NOT throttling --------------------------------
    code, _h, body = _post(base, "/eligibility",
                           {"member_id": "NOSUCHMEMBER", "last_name": "NOBODY"},
                           partner="SUBMITTERA")
    js = json.loads(body)
    rej = js.get("rejection", {})
    print("")
    print(f"  an unknown member -> {code}, AAA {rej.get('code')} "
          f"'{rej.get('reason')}' / {rej.get('action')}")
    print(f"    answered: {js.get('answered')}   "
          f"determination: {js.get('benefit_determination')}")
    print("    HTTP 200 with answered=false, and that is deliberate. The")
    print("    transaction succeeded; the ANSWER is a rejection. A 404")
    print("    would conflate 'we could not process your inquiry' with")
    print("    'this member does not exist', and the front desk needs the")
    print("    second one so they can correct the ID and resubmit.")

    # ---- the dashboard -----------------------------------------------------
    _c, body = _get(base, "/dashboard")
    d = json.loads(body)
    s = d["transactions"]
    print("\n" + "-" * 76)
    print("DASHBOARD")
    print("-" * 76)
    print(f"  transactions {s['total']}   rejection rate "
          f"{s['rejection_rate']:.1%}   p95 {s['response_ms']['p95']:.1f} ms")
    print(f"  {'AAA':<6}{'reason':<46}{'n':>4}{'share':>8}")
    for r in s["by_aaa"]:
        print(f"  {r['code']:<6}{r['reason'][:44]:<46}{r['n']:>4}"
              f"{r['share']:>8.1%}")
    print("\n  BY CODE, NOT AS ONE RATE. 67 is the submitter sending bad")
    print("  member IDs; 42 is us throttling them. Different phone calls, and")
    print("  a single rejection-rate figure tells operations neither.")
    print(f"\n  {'partner':<14}{'agreed':>9}{'allowed':>9}{'throttled':>11}"
          f"{'TPA':>6}")
    for r in d["throttle"]:
        print(f"  {r['partner']:<14}{r['agreed_per_hour']:>8,}h{r['allowed']:>9,}"
              f"{r['throttled']:>11,}{'yes' if r['has_agreement'] else 'NO':>6}")

    _c, html = _get(base, "/dashboard.html")
    os.makedirs("out", exist_ok=True)
    with open("out/dashboard.html", "wb") as fh:
        fh.write(html)
    print(f"\n  wrote out/dashboard.html ({len(html):,} bytes)")

    httpd.shutdown()
    return True


if __name__ == "__main__":
    main()
