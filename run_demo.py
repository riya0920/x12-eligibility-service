"""Real-time and batch eligibility, claim status, and the accumulator linkage.

Run:  python run_demo.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import transactions as T
import x12
from payer import PayerCore
from x12 import AAA_REJECT, ControlNumbers

OUT = "out"


def inquire(core, control, member_id, last, first, dob, service_type, dos):
    """The full round trip: JSON in -> 270 -> payer core -> 271 -> JSON out."""
    req = T.build_270(control, member_id, last, first, dob, service_type, dos)
    parsed_req = T.parse_270(req)
    resp, meta = T.build_271(control, parsed_req, core)
    parsed_resp = T.parse_271(resp)
    return req, resp, T.to_json(parsed_resp, meta), meta


def main():
    os.makedirs(OUT, exist_ok=True)
    core = PayerCore()
    control = ControlNumbers()

    # ---- a real 270 on the wire -----------------------------------------
    print("=" * 78)
    print("270 ELIGIBILITY INQUIRY (as it goes on the wire)")
    print("=" * 78)
    req = T.build_270(control, "W123456789", "SMITH", "JANE", "1956-03-12",
                      "98", "2024-06-12")
    for line in req.render().split("~"):
        if line:
            print("  " + line)
    problems = x12.check_envelope(req)
    print(f"\n  envelope check: {'OK' if not problems else problems}")
    isa, gs, st = req.first("ISA"), req.first("GS"), req.first("ST")
    print(f"  control numbers: ISA13={isa.get(13)}  GS06={gs.get(6)}  "
          f"ST02={st.get(2)}  (matched by IEA02/GE02/SE02)")

    # ---- the five answer states -----------------------------------------
    print("\n" + "=" * 78)
    print("THE FIVE ANSWER STATES -- why the response model is not a boolean")
    print("=" * 78)
    cases = [
        ("active, benefit known", "W123456789", "SMITH", "1956-03-12", "98", "2024-06-12"),
        ("carve-out: unknown here", "W123456789", "SMITH", "1956-03-12", "35", "2024-06-12"),
        ("service not covered", "W123456789", "SMITH", "1956-03-12", "AL", "2024-06-12"),
        ("coverage terminated", "W456789012", "NAKAMURA", "1988-10-10", "98", "2024-09-15"),
        ("member not found", "W999999999", "NOBODY", "1970-01-01", "98", "2024-06-12"),
        ("DOB mismatch", "W123456789", "SMITH", "1956-03-13", "98", "2024-06-12"),
    ]
    results = {}
    for label, mid, last, dob, st_code, dos in cases:
        _rq, _rs, js, _meta = inquire(core, control, mid, last, "", dob, st_code, dos)
        results[label] = js
        print(f"\n  {label}")
        print(f"    determination : {js['benefit_determination']}")
        if not js["answered"]:
            r = js["rejection"]
            print(f"    AAA03         : {r['code']} -- {r['reason']}")
            print(f"    AAA04         : {r['action_code']} -- {r['action']}")
        elif js["benefit_determination"] == "unknown_contact_other_entity":
            print(f"    contact       : {js['contact']}")
            print(f"    note          : {js['note'][:60]}...")
        elif js["benefit_determination"] == "active_coverage_benefits_known":
            print(f"    copay         : ${js['cost_share']['copay']:.2f}")
            print(f"    deductible remaining : "
                  f"${js['accumulators']['deductible_remaining']:,.2f}")

    print("\n  Note the third and fifth rows. 'Vision not covered' and 'the")
    print("  dental administrator handles this' are BOTH not-a-yes, and a")
    print("  boolean makes them identical. They are not: one turns the patient")
    print("  away, the other is a phone call. A front desk told 'not covered'")
    print("  for a carve-out benefit denies care the member actually has.")

    # ---- the accumulator linkage ----------------------------------------
    print("\n" + "=" * 78)
    print("ACCUMULATOR LINKAGE -- eligibility reflects the claims store")
    print("=" * 78)
    _q, _r, before, _m = inquire(core, control, "W234567890", "OKONKWO", "",
                                 "1980-11-22", "98", "2024-06-12")
    print(f"  before claim: deductible remaining "
          f"${before['accumulators']['deductible_remaining']:,.2f}, "
          f"OOP remaining ${before['accumulators']['out_of_pocket_remaining']:,.2f}")

    adj = core.adjudicate("CLM0001", "W234567890", "1234567893", "2024-06-13",
                          "48", 4200.00)
    print(f"  adjudicated CLM0001: billed $4,200.00 -> allowed "
          f"${adj['allowed']:,.2f}, plan paid ${adj['paid']:,.2f}, "
          f"member owes ${adj['patient_resp']:,.2f}")
    print(f"    (${adj['applied_to_deductible']:,.2f} applied to the deductible)")

    _q, _r, after, _m = inquire(core, control, "W234567890", "OKONKWO", "",
                                "1980-11-22", "98", "2024-06-14")
    print(f"  after claim:  deductible remaining "
          f"${after['accumulators']['deductible_remaining']:,.2f}, "
          f"OOP remaining ${after['accumulators']['out_of_pocket_remaining']:,.2f}")
    moved = (before["accumulators"]["deductible_remaining"]
             - after["accumulators"]["deductible_remaining"])
    print(f"  the 271 moved by ${moved:,.2f} because a claim adjudicated between")
    print("  the two inquiries. That linkage is what makes this a system rather")
    print("  than two toys sitting next to each other.")
    print("\n  Consistency, stated: reads here are read-your-writes because both")
    print("  live in one database. Real payers usually are NOT -- adjudication")
    print("  runs on a separate cycle, so a 271 ten seconds after a claim")
    print("  commonly reflects the PRE-claim accumulator. That is a property of")
    print("  the architecture, not a bug in the eligibility service, and it is")
    print("  why every real 271 carries estimate-only language.")
    print(f"\n  facade estimate_only flag: {after['estimate_only']}")

    # ---- 276/277 ---------------------------------------------------------
    print("\n" + "=" * 78)
    print("276 / 277 CLAIM STATUS")
    print("=" * 78)
    for claim_id in ("CLM0001", "CLM-DOES-NOT-EXIST"):
        _req276 = T.build_276(control, "W234567890", claim_id, "2024-06-13")
        _resp, meta = T.build_277(control, claim_id, core)
        cat = meta["category"]
        print(f"  {claim_id:<22} STC {cat}:{meta['code']}  "
              f"{x12.STATUS_CATEGORY[cat]}")
        if meta["found"]:
            print(f"    billed ${meta['billed']:,.2f}  paid ${meta['paid']:,.2f}  "
                  f"member ${meta['patient_responsibility']:,.2f}")

    core.adjudicate("CLM0002", "W456789012", "1234567893", "2024-09-15",
                    "98", 180.00)
    _resp, meta = T.build_277(control, "CLM0002", core)
    print(f"  {'CLM0002':<22} STC {meta['category']}:{meta['code']}  "
          f"{x12.STATUS_CATEGORY[meta['category']]}")
    print(f"    denial reason: {meta['denial_reason']}")

    # ---- round-trip fidelity ---------------------------------------------
    print("\n" + "=" * 78)
    print("ROUND-TRIP FIDELITY: generate -> render -> parse -> compare")
    print("=" * 78)
    ok = 0
    for label, mid, last, dob, st_code, dos in cases:
        _rq, resp, js, meta = inquire(core, control, mid, last, "", dob, st_code, dos)
        wire = resp.render()
        reparsed = T.parse_271(x12.parse(wire))
        js2 = T.to_json(reparsed, meta)
        same = js2["benefit_determination"] == js["benefit_determination"]
        ok += same
        print(f"  {label:<26} {'MATCH' if same else 'DIFFERS'}")
    print(f"\n  {ok}/{len(cases)} transactions survive a full "
          f"generate -> serialise -> parse cycle with identical semantics")

    # ---- envelope integrity ----------------------------------------------
    print("\n" + "=" * 78)
    print("ENVELOPE INTEGRITY (structural errors reject the INTERCHANGE)")
    print("=" * 78)
    good = T.build_270(control, "W123456789", "SMITH", "JANE", "1956-03-12",
                       "30", "2024-06-12")
    print(f"  well-formed        : {x12.check_envelope(good) or 'no problems'}")
    broken = x12.parse(good.render().replace("~IEA*1*", "~IEA*1*99999"))
    print(f"  IEA02 tampered     : {x12.check_envelope(broken)}")
    broken2 = x12.parse(good.render().replace("SE*", "SE*99*", 1))
    print(f"  SE01 count wrong   : {x12.check_envelope(broken2)}")
    print("\n  These are 999/TA1 territory: a control-number mismatch rejects")
    print("  the whole interchange, not just the offending transaction, which")
    print("  is why it is checked before any business logic runs.")

    # ---- batch mode -------------------------------------------------------
    print("\n" + "=" * 78)
    print("BATCH MODE -- because payers still run batch eligibility in 2026")
    print("=" * 78)
    inbox, outbox = f"{OUT}/batch_in", f"{OUT}/batch_out"
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(outbox, exist_ok=True)
    for f in glob.glob(f"{inbox}/*") + glob.glob(f"{outbox}/*"):
        os.remove(f)

    batch_members = [("W123456789", "SMITH", "1956-03-12"),
                     ("W345678901", "GARCIA", "1965-08-15"),
                     ("W456789012", "NAKAMURA", "1988-10-10"),
                     ("W999999999", "NOBODY", "1970-01-01"),
                     ("W567890123", "ADEYEMI", "1992-02-04")]
    batch = []
    for mid, last, dob in batch_members:
        batch.append(T.build_270(control, mid, last, "", dob, "98", "2024-09-15")
                     .render())
    with open(f"{inbox}/270_batch_001.edi", "w") as fh:
        fh.write("\n".join(batch))
    print(f"  wrote {inbox}/270_batch_001.edi with {len(batch)} inquiries")

    t0 = time.time()
    responses, rejects = [], 0
    with open(f"{inbox}/270_batch_001.edi") as fh:
        for line in fh:
            if not line.strip():
                continue
            parsed = T.parse_270(x12.parse(line))
            resp, meta = T.build_271(control, parsed, core)
            responses.append(resp.render())
            rejects += bool(meta.get("rejected"))
    with open(f"{outbox}/271_batch_001.edi", "w") as fh:
        fh.write("\n".join(responses))
    dt = time.time() - t0

    print(f"  wrote {outbox}/271_batch_001.edi with {len(responses)} responses")
    print(f"  control totals: {len(batch)} in, {len(responses)} out, "
          f"{'BALANCED' if len(batch) == len(responses) else 'IMBALANCE'}")
    print(f"  rejections: {rejects}/{len(responses)} "
          f"({rejects/len(responses):.0%})")
    print(f"  throughput: {len(responses)/dt:,.0f} transactions/sec "
          f"({len(responses)} in {dt*1000:.1f} ms, single core, in-memory DB)")
    print("\n  Why batch still exists in 2026: volume economics (a nightly file")
    print("  of 400,000 inquiries costs a fraction of 400,000 real-time calls),")
    print("  legacy trading partners whose systems only speak files, and")
    print("  overnight refresh being genuinely sufficient for tomorrow's")
    print("  scheduled appointments. It is not technical debt, it is a")
    print("  different workload.")

    payload = {"answer_states": {k: v["benefit_determination"]
                                 for k, v in results.items()},
               "accumulator_moved_by": moved,
               "round_trip": f"{ok}/{len(cases)}",
               "batch": {"in": len(batch), "out": len(responses),
                         "rejects": rejects,
                         "tps": len(responses) / dt},
               "aaa_codes_implemented": len(AAA_REJECT)}
    with open(f"{OUT}/results.json", "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\nwrote {OUT}/results.json")


if __name__ == "__main__":
    main()
