"""270/271 eligibility and 276/277 claim status, plus the ambiguity-preserving
JSON facade.

THE DESIGN INSIGHT THE WHOLE PROJECT HANGS ON
---------------------------------------------
Eligibility is a QUESTION with a structured, partial, frequently ambiguous
ANSWER. A facade that returns `{"eligible": true}` has thrown away the part
that matters.

The real answer space has at least five distinct states, and X12 has codes for
all of them:

    active coverage, benefit known           EB01=1 with EB07/EB08 amounts
    active coverage, benefit NOT known here  EB01=U  "contact following entity"
    active coverage, service not covered     EB01=I  non-covered
    coverage inactive on the date of service EB01=6
    cannot answer -- the inquiry rejected    AAA segment, no EB at all

Collapsing those into a boolean loses: whether the front desk should collect a
copay, whether they should call the carve-out administrator, whether the
patient has other coverage, and whether the answer is a fact or an estimate. So
`to_json()` preserves all five, and `benefit_determination` is a STRING, never
a boolean.

The facade also never invents certainty. Where the payer core does not know, the
response says `unknown` and carries the follow-up entity -- because a front desk
told "not covered" turns a patient away, while a front desk told "contact the
dental administrator" makes a phone call.
"""

from __future__ import annotations

from datetime import date

import x12
from x12 import AAA_ACTION, AAA_REJECT, EB01_CODES, Segment, SERVICE_TYPES


# ---------------------------------------------------------------------------
# 270 -- eligibility inquiry
# ---------------------------------------------------------------------------
def build_270(control, member_id, last_name, first_name, dob, service_type,
              service_date, provider_npi="1234567893",
              provider_name="RIVERSIDE FAMILY MEDICINE"):
    segs = [
        Segment("BHT", "0022", "13", "TRN" + control.st(),
                service_date.replace("-", ""), "1015"),
        # Loop 2100A -- information source (the payer)
        Segment("HL", "1", "", "20", "1"),
        Segment("NM1", "PR", "2", "DEMO HEALTH PLAN", "", "", "", "", "PI", "PAYERID"),
        # Loop 2100B -- information receiver (the provider)
        Segment("HL", "2", "1", "21", "1"),
        Segment("NM1", "1P", "2", provider_name, "", "", "", "", "XX", provider_npi),
        # Loop 2100C -- subscriber
        Segment("HL", "3", "2", "22", "0"),
        Segment("TRN", "1", "TRACE" + control.st(), "9" + provider_npi),
        Segment("NM1", "IL", "1", last_name, first_name, "", "", "", "MI", member_id),
        Segment("DMG", "D8", (dob or "").replace("-", "")),
        Segment("DTP", "291", "D8", service_date.replace("-", "")),
        Segment("EQ", service_type),
    ]
    return x12.envelope(segs, "HS", "270", control)


def parse_270(interchange):
    nm1 = [s for s in interchange.find("NM1") if s.get(1) == "IL"]
    subscriber = nm1[0] if nm1 else None
    dmg = interchange.first("DMG")
    dtp = interchange.first("DTP")
    eq = interchange.first("EQ")
    provider = [s for s in interchange.find("NM1") if s.get(1) == "1P"]
    raw_date = dtp.get(3) if dtp else ""
    return {
        "member_id": subscriber.get(9) if subscriber else "",
        "last_name": subscriber.get(3) if subscriber else "",
        "first_name": subscriber.get(4) if subscriber else "",
        "dob": dmg.get(2) if dmg else "",
        "service_type": eq.get(1) if eq else "30",
        "service_date": (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                         if len(raw_date) == 8 else ""),
        "provider_npi": provider[0].get(9) if provider else "",
        "trace": (interchange.first("TRN").get(2)
                  if interchange.first("TRN") else ""),
    }


# ---------------------------------------------------------------------------
# 271 -- eligibility response
# ---------------------------------------------------------------------------
def build_271(control, inquiry, core):
    """Answer a parsed 270 against the payer core. Returns (interchange, meta)."""
    member_id = inquiry["member_id"]
    row, aaa = core.find_member(member_id, inquiry.get("last_name"),
                                inquiry.get("dob"))

    header = [
        Segment("BHT", "0022", "11", inquiry.get("trace", "TRN1"),
                (inquiry.get("service_date") or "2024-06-12").replace("-", ""),
                "1015"),
        Segment("HL", "1", "", "20", "1"),
        Segment("NM1", "PR", "2", "DEMO HEALTH PLAN", "", "", "", "", "PI", "PAYERID"),
        Segment("HL", "2", "1", "21", "1"),
        Segment("NM1", "1P", "2", "PROVIDER", "", "", "", "", "XX",
                inquiry.get("provider_npi", "")),
        Segment("HL", "3", "2", "22", "0"),
    ]
    meta = {"member_id": member_id, "service_type": inquiry.get("service_type"),
            "service_date": inquiry.get("service_date")}

    # ---- rejection path: no EB segments at all, an AAA instead ----------
    if aaa:
        name = ["NM1", "IL", "1", inquiry.get("last_name", ""),
                inquiry.get("first_name", ""), "", "", "", "MI", member_id]
        action = {"75": "C", "71": "C", "73": "C", "67": "C"}.get(aaa, "C")
        segs = header + [
            Segment(*name),
            Segment("AAA", "Y", "", aaa, action),
        ]
        meta.update({"rejected": True, "aaa_code": aaa,
                     "aaa_reason": AAA_REJECT[aaa],
                     "aaa_action": AAA_ACTION[action]})
        return x12.envelope(segs, "HB", "271", control), meta

    _mid, first, last, dob, gender, group, plan_id = row
    service_date = inquiry.get("service_date") or date.today().isoformat()
    cov = core.coverage_on(member_id, service_date)
    segs = header + [
        Segment("NM1", "IL", "1", last, first, "", "", "", "MI", member_id),
        Segment("REF", "6P", group),
        Segment("DMG", "D8", dob, gender),
        Segment("DTP", "346", "D8", cov.get("start", "").replace("-", "")),
    ]

    # ---- inactive coverage: EB01=6, and say WHEN it ended ---------------
    if not cov.get("active"):
        segs.append(Segment("EB", "6", "IND", inquiry.get("service_type", "30")))
        if cov.get("end"):
            segs.append(Segment("DTP", "347", "D8", cov["end"].replace("-", "")))
        meta.update({"rejected": False, "active": False,
                     "eb01": "6", "reason": cov.get("reason")})
        return x12.envelope(segs, "HB", "271", control), meta

    st = inquiry.get("service_type", "30")
    ben = core.benefit(cov["plan_id"], st)
    acc = core.accumulators(member_id)

    # ---- benefit not known here: EB01=U, NOT "not covered" -------------
    if not ben["known"]:
        segs.append(Segment("EB", "U", "IND", st, "", "", "", "", "", "", "",
                            "", "Y"))
        segs.append(Segment("LS", "2120"))
        segs.append(Segment("NM1", "13", "2", "DENTAL BENEFITS ADMINISTRATOR",
                            "", "", "", "", "PI", "DBA001"))
        segs.append(Segment("LE", "2120"))
        meta.update({"rejected": False, "active": True, "eb01": "U",
                     "benefit_known": False,
                     "follow_up_entity": "DENTAL BENEFITS ADMINISTRATOR"})
        return x12.envelope(segs, "HB", "271", control), meta

    # ---- non-covered service: EB01=I -----------------------------------
    if not ben["covered"]:
        segs.append(Segment("EB", "I", "IND", st))
        meta.update({"rejected": False, "active": True, "eb01": "I",
                     "benefit_known": True, "covered": False})
        return x12.envelope(segs, "HB", "271", control), meta

    # ---- active coverage with benefits ---------------------------------
    segs.append(Segment("EB", "1", "IND", "30", plan_id))
    if ben.get("copay"):
        segs.append(Segment("EB", "B", "IND", st, "", "27", str(ben["copay"])))
    if ben.get("coinsurance"):
        segs.append(Segment("EB", "A", "IND", st, "", "27", "",
                            f"{ben['coinsurance']:.2f}"))
    segs.append(Segment("EB", "C", "IND", "30", "", "23",
                        f"{acc['deductible_total']:.2f}"))
    segs.append(Segment("EB", "C", "IND", "30", "", "29",
                        f"{acc['deductible_remaining']:.2f}"))
    segs.append(Segment("EB", "G", "IND", "30", "", "23",
                        f"{acc['oop_max_total']:.2f}"))
    segs.append(Segment("EB", "G", "IND", "30", "", "29",
                        f"{acc['oop_remaining']:.2f}"))
    meta.update({"rejected": False, "active": True, "eb01": "1",
                 "benefit_known": True, "covered": True,
                 "copay": ben.get("copay"), "coinsurance": ben.get("coinsurance"),
                 "accumulators": acc, "plan_id": plan_id})
    return x12.envelope(segs, "HB", "271", control), meta


def parse_271(interchange):
    """Parse a 271 back into structured data. Used for round-trip fidelity."""
    out = {"rejected": False, "benefits": [], "aaa": None}
    aaa = interchange.first("AAA")
    if aaa:
        out["rejected"] = True
        out["aaa"] = {"valid": aaa.get(1), "code": aaa.get(3),
                      "reason": AAA_REJECT.get(aaa.get(3), "unknown"),
                      "action_code": aaa.get(4),
                      "action": AAA_ACTION.get(aaa.get(4), "")}
        return out
    for eb in interchange.find("EB"):
        out["benefits"].append({
            "eb01": eb.get(1), "eb01_meaning": EB01_CODES.get(eb.get(1), "?"),
            "coverage_level": eb.get(2),
            "service_type": eb.get(3),
            "service_type_name": SERVICE_TYPES.get(eb.get(3), ""),
            "plan": eb.get(4),
            "time_period": eb.get(6),
            "amount": eb.get(7), "percent": eb.get(8),
        })
    nm1 = [s for s in interchange.find("NM1") if s.get(1) == "IL"]
    if nm1:
        out["member_id"] = nm1[0].get(9)
        out["last_name"] = nm1[0].get(3)
    follow_up = [s for s in interchange.find("NM1") if s.get(1) == "13"]
    if follow_up:
        out["follow_up_entity"] = follow_up[0].get(3)
    return out


# ---------------------------------------------------------------------------
# The JSON facade -- ambiguity preserved
# ---------------------------------------------------------------------------
def to_json(parsed_271, meta):
    """Translate a 271 into clean JSON that does NOT flatten the answer.

    `benefit_determination` is a string with five possible values, never a
    boolean, because the five states have different operational consequences
    at a provider's front desk.
    """
    if parsed_271["rejected"]:
        return {
            "answered": False,
            "benefit_determination": "inquiry_rejected",
            "rejection": parsed_271["aaa"],
            "what_the_front_desk_should_do": parsed_271["aaa"]["action"],
            "as_of": meta.get("service_date"),
        }

    eb01 = meta.get("eb01")
    determination = {
        "1": "active_coverage_benefits_known",
        "6": "coverage_inactive_on_date_of_service",
        "U": "unknown_contact_other_entity",
        "I": "service_not_covered",
    }.get(eb01, "unknown")

    out = {
        "answered": True,
        "benefit_determination": determination,
        "member_id": parsed_271.get("member_id"),
        "service_type": meta.get("service_type"),
        "service_type_name": SERVICE_TYPES.get(meta.get("service_type"), ""),
        "as_of": meta.get("service_date"),
        "estimate_only": True,
        "estimate_caveat":
            "Accumulators reflect claims adjudicated at the time of this "
            "inquiry. Claims in flight are not included, so remaining "
            "deductible and out-of-pocket are point-in-time estimates and "
            "must not be quoted to a member as a final amount.",
    }
    if determination == "coverage_inactive_on_date_of_service":
        out["coverage_end_reason"] = meta.get("reason")
    if determination == "unknown_contact_other_entity":
        out["contact"] = parsed_271.get("follow_up_entity") or \
            meta.get("follow_up_entity")
        out["note"] = ("The payer does not administer this benefit. This is "
                       "NOT a denial and must not be presented to the member "
                       "as one.")
    if determination == "active_coverage_benefits_known":
        out["cost_share"] = {"copay": meta.get("copay"),
                             "coinsurance": meta.get("coinsurance")}
        acc = meta.get("accumulators") or {}
        out["accumulators"] = {
            "deductible_total": acc.get("deductible_total"),
            "deductible_remaining": acc.get("deductible_remaining"),
            "out_of_pocket_max": acc.get("oop_max_total"),
            "out_of_pocket_remaining": acc.get("oop_remaining"),
        }
    return out


# ---------------------------------------------------------------------------
# 276 / 277 -- claim status
# ---------------------------------------------------------------------------
def build_276(control, member_id, claim_id, service_date,
              provider_npi="1234567893"):
    segs = [
        Segment("BHT", "0010", "13", "CS" + control.st(),
                service_date.replace("-", ""), "1015"),
        Segment("HL", "1", "", "20", "1"),
        Segment("NM1", "PR", "2", "DEMO HEALTH PLAN", "", "", "", "", "PI", "PAYERID"),
        Segment("HL", "2", "1", "21", "1"),
        Segment("NM1", "41", "2", "PROVIDER", "", "", "", "", "XX", provider_npi),
        Segment("HL", "3", "2", "22", "0"),
        Segment("NM1", "IL", "1", "", "", "", "", "", "MI", member_id),
        Segment("TRN", "1", "CSTRACE" + control.st()),
        Segment("REF", "1K", claim_id),
        Segment("DTP", "232", "D8", service_date.replace("-", "")),
    ]
    return x12.envelope(segs, "HR", "276", control)


def build_277(control, claim_id, core):
    row = core.claim(claim_id=claim_id)
    segs = [
        Segment("BHT", "0010", "08", "CS1", "20240612", "1015"),
        Segment("HL", "1", "", "20", "1"),
        Segment("NM1", "PR", "2", "DEMO HEALTH PLAN", "", "", "", "", "PI", "PAYERID"),
        Segment("HL", "2", "1", "21", "1"),
        Segment("HL", "3", "2", "22", "0"),
    ]
    if row is None:
        segs += [Segment("TRN", "2", claim_id),
                 Segment("STC", "A4:35", "20240612", "", "")]
        return x12.envelope(segs, "HN", "277", control), {
            "found": False, "category": "A4", "code": "35"}
    (cid, member_id, svc_date, _st, billed, allowed, paid, resp,
     cat, code, denial) = row
    segs += [
        Segment("NM1", "IL", "1", "", "", "", "", "", "MI", member_id),
        Segment("TRN", "2", cid),
        Segment("STC", f"{cat}:{code}", svc_date.replace("-", ""), "",
                f"{billed:.2f}", f"{paid:.2f}"),
        Segment("REF", "1K", cid),
        Segment("DTP", "232", "D8", svc_date.replace("-", "")),
    ]
    return x12.envelope(segs, "HN", "277", control), {
        "found": True, "category": cat, "code": code, "billed": billed,
        "allowed": allowed, "paid": paid, "patient_responsibility": resp,
        "denial_reason": denial}
