"""The simulated payer core: members, coverage, benefits with accumulators,
and a claims store that MOVES those accumulators.

THE LINKAGE IS WHAT MAKES THIS A SYSTEM
---------------------------------------
Two toys sit next to each other in most projects: an eligibility lookup that
returns static benefit amounts, and a claims table nobody queries. Wiring them
together is the systems-thinking move, because in reality a 271 answers a
question about a MOVING TARGET -- "how much deductible is left" changes every
time a claim adjudicates, and the provider's front desk is quoting a patient a
number that may be stale by the time they walk out.

So: `adjudicate()` applies a claim against the member's accumulators, and the
next 271 reflects it. `test_accumulator_moves_the_next_271` proves the link.

CONSISTENCY CHOICE, MADE EXPLICITLY
-----------------------------------
Eligibility reads are READ-YOUR-WRITES consistent with the claims store here,
because both are the same SQLite database and adjudication commits before the
next inquiry runs.

Real payers usually are NOT. Adjudication runs in a separate system on a batch
or near-real-time cycle, so a 271 issued ten seconds after a claim adjudicates
commonly reflects the PRE-claim accumulator. That is not a bug to be fixed by
the eligibility service; it is a property of the architecture, and the honest
response is that a 271 is a point-in-time estimate and every real one carries
language to that effect. Quoting a patient an exact remaining deductible from a
271 is where provider billing offices get into trouble.
"""

from __future__ import annotations

import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE member (
    member_id   TEXT PRIMARY KEY,
    first_name  TEXT, last_name TEXT, dob TEXT, gender TEXT,
    group_number TEXT, plan_id TEXT
);
CREATE TABLE coverage_span (
    member_id  TEXT, span_start TEXT, span_end TEXT, plan_id TEXT
);
CREATE TABLE benefit (
    plan_id      TEXT, service_type TEXT,
    covered      INTEGER,          -- 0 = non-covered, 1 = covered, 2 = unknown
    copay        REAL, coinsurance REAL,
    requires_auth INTEGER DEFAULT 0
);
CREATE TABLE accumulator (
    member_id       TEXT, benefit_year TEXT,
    deductible_total REAL, deductible_met REAL,
    oop_max_total    REAL, oop_met REAL,
    PRIMARY KEY (member_id, benefit_year)
);
CREATE TABLE claim (
    claim_id     TEXT PRIMARY KEY,
    member_id    TEXT, provider_npi TEXT, service_date TEXT,
    service_type TEXT, billed REAL, allowed REAL, paid REAL,
    patient_resp REAL, status_category TEXT, status_code TEXT,
    denial_reason TEXT
);
"""

PLANS = {
    "PPO-GOLD": {"deductible": 1500.0, "oop_max": 6000.0,
                 "benefits": {"30": (1, 0.0, 0.0), "98": (1, 25.0, 0.0),
                              "86": (1, 250.0, 0.20), "48": (1, 0.0, 0.20),
                              "50": (1, 0.0, 0.20), "88": (1, 10.0, 0.0),
                              "MH": (1, 25.0, 0.0), "AL": (0, 0.0, 0.0),
                              "35": (2, 0.0, 0.0)}},
    "HMO-BASIC": {"deductible": 3000.0, "oop_max": 8000.0,
                  "benefits": {"30": (1, 0.0, 0.0), "98": (1, 40.0, 0.0),
                               "86": (1, 500.0, 0.30), "48": (1, 0.0, 0.30),
                               "50": (1, 0.0, 0.30), "88": (1, 20.0, 0.0),
                               "MH": (1, 40.0, 0.0), "AL": (0, 0.0, 0.0),
                               "35": (2, 0.0, 0.0)}},
}


class PayerCore:
    def __init__(self, path=":memory:"):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.executescript(SCHEMA)
        self._seed()

    def _seed(self):
        members = [
            # id, first, last, dob, gender, group, plan, span_start, span_end
            ("W123456789", "JANE", "SMITH", "19560312", "F", "GRP001",
             "PPO-GOLD", "2024-01-01", "2024-12-31"),
            ("W234567890", "ROBERT", "OKONKWO", "19801122", "M", "GRP001",
             "PPO-GOLD", "2024-01-01", "2024-12-31"),
            ("W345678901", "MARIA", "GARCIA", "19650815", "F", "GRP002",
             "HMO-BASIC", "2024-01-01", "2024-12-31"),
            # coverage TERMINATED mid-year -- the 271 must say so
            ("W456789012", "KEN", "NAKAMURA", "19881010", "M", "GRP002",
             "HMO-BASIC", "2024-01-01", "2024-06-30"),
            # coverage starts later in the year
            ("W567890123", "CHIDINMA", "ADEYEMI", "19920204", "F", "GRP003",
             "PPO-GOLD", "2024-08-01", "2024-12-31"),
        ]
        for mid, fn, ln, dob, g, grp, plan, s, e in members:
            self.con.execute("INSERT INTO member VALUES (?,?,?,?,?,?,?)",
                             (mid, fn, ln, dob, g, grp, plan))
            self.con.execute("INSERT INTO coverage_span VALUES (?,?,?,?)",
                             (mid, s, e, plan))
            p = PLANS[plan]
            self.con.execute("INSERT INTO accumulator VALUES (?,?,?,?,?,?)",
                             (mid, "2024", p["deductible"], 0.0,
                              p["oop_max"], 0.0))
        for plan_id, p in PLANS.items():
            for st, (covered, copay, coins) in p["benefits"].items():
                self.con.execute("INSERT INTO benefit VALUES (?,?,?,?,?,?)",
                                 (plan_id, st, covered, copay, coins, 0))
        self.con.commit()

    # -- lookups ---------------------------------------------------------
    def find_member(self, member_id=None, last_name=None, dob=None):
        """Returns (member_row, aaa_code) -- the rejection reason IS the result
        when the lookup fails, and distinguishing 'not found' from 'found but
        DOB mismatch' is what makes a 271 actionable."""
        row = None
        if member_id:
            row = self.con.execute(
                "SELECT member_id, first_name, last_name, dob, gender, "
                "group_number, plan_id FROM member WHERE member_id=?",
                (member_id,)).fetchone()
        if row is None:
            return None, "75"                       # subscriber not found
        if dob and row[3] != dob.replace("-", ""):
            return row, "71"                        # DOB does not match
        if last_name and row[2].upper() != last_name.upper():
            return row, "73"                        # name mismatch
        return row, None

    def coverage_on(self, member_id, service_date):
        rows = self.con.execute(
            "SELECT span_start, span_end, plan_id FROM coverage_span "
            "WHERE member_id=?", (member_id,)).fetchall()
        for s, e, plan in rows:
            if s <= service_date <= e:
                return {"active": True, "start": s, "end": e, "plan_id": plan}
        if rows:
            s, e, plan = rows[0]
            return {"active": False, "start": s, "end": e, "plan_id": plan,
                    "reason": "terminated" if service_date > e else "not yet effective"}
        return {"active": False, "reason": "no coverage on file"}

    def benefit(self, plan_id, service_type):
        row = self.con.execute(
            "SELECT covered, copay, coinsurance FROM benefit "
            "WHERE plan_id=? AND service_type=?", (plan_id, service_type)
        ).fetchone()
        if row is None:
            # Not in the benefit table is NOT "not covered". It is unknown, and
            # saying so is the entire point of EB01=U.
            return {"known": False, "covered": None}
        covered, copay, coins = row
        if covered == 2:
            return {"known": False, "covered": None,
                    "note": "carve-out administered by another entity"}
        return {"known": True, "covered": bool(covered), "copay": copay,
                "coinsurance": coins}

    def accumulators(self, member_id, year="2024"):
        row = self.con.execute(
            "SELECT deductible_total, deductible_met, oop_max_total, oop_met "
            "FROM accumulator WHERE member_id=? AND benefit_year=?",
            (member_id, year)).fetchone()
        if not row:
            return None
        dt, dm, ot, om = row
        return {"deductible_total": dt, "deductible_met": dm,
                "deductible_remaining": max(0.0, dt - dm),
                "oop_max_total": ot, "oop_met": om,
                "oop_remaining": max(0.0, ot - om)}

    # -- adjudication ----------------------------------------------------
    def adjudicate(self, claim_id, member_id, provider_npi, service_date,
                   service_type, billed):
        """Apply a claim and MOVE the accumulators. The next 271 sees this."""
        cov = self.coverage_on(member_id, service_date)
        if not cov.get("active"):
            self.con.execute(
                "INSERT OR REPLACE INTO claim VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, member_id, provider_npi, service_date, service_type,
                 billed, 0.0, 0.0, billed, "F2", "88",
                 f"no active coverage on the date of service ({cov.get('reason')})"))
            self.con.commit()
            return {"status": "denied", "paid": 0.0, "patient_resp": billed}

        ben = self.benefit(cov["plan_id"], service_type)
        allowed = round(billed * 0.62, 2)           # contracted rate
        if ben["known"] and not ben["covered"]:
            self.con.execute(
                "INSERT OR REPLACE INTO claim VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, member_id, provider_npi, service_date, service_type,
                 billed, allowed, 0.0, allowed, "F2", "88",
                 "service type is not a covered benefit under this plan"))
            self.con.commit()
            return {"status": "denied", "paid": 0.0, "patient_resp": allowed}

        acc = self.accumulators(member_id)
        copay = ben.get("copay", 0.0) or 0.0
        coins_rate = ben.get("coinsurance", 0.0) or 0.0

        remaining_ded = acc["deductible_remaining"]
        to_deductible = min(remaining_ded, max(0.0, allowed - copay))
        after_ded = max(0.0, allowed - copay - to_deductible)
        coinsurance = round(after_ded * coins_rate, 2)
        patient_resp = round(copay + to_deductible + coinsurance, 2)

        # out-of-pocket maximum caps what the member can be charged
        oop_remaining = acc["oop_remaining"]
        if patient_resp > oop_remaining:
            patient_resp = round(oop_remaining, 2)
        paid = round(allowed - patient_resp, 2)

        self.con.execute(
            "UPDATE accumulator SET deductible_met = deductible_met + ?, "
            "oop_met = oop_met + ? WHERE member_id=? AND benefit_year='2024'",
            (to_deductible, patient_resp, member_id))
        self.con.execute(
            "INSERT OR REPLACE INTO claim VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (claim_id, member_id, provider_npi, service_date, service_type,
             billed, allowed, paid, patient_resp, "F1", "65", None))
        self.con.commit()
        return {"status": "paid", "allowed": allowed, "paid": paid,
                "patient_resp": patient_resp, "applied_to_deductible": to_deductible}

    def claim(self, claim_id=None, member_id=None):
        if claim_id:
            return self.con.execute(
                "SELECT claim_id, member_id, service_date, service_type, billed,"
                " allowed, paid, patient_resp, status_category, status_code,"
                " denial_reason FROM claim WHERE claim_id=?",
                (claim_id,)).fetchone()
        return self.con.execute(
            "SELECT claim_id, member_id, service_date, service_type, billed,"
            " allowed, paid, patient_resp, status_category, status_code,"
            " denial_reason FROM claim WHERE member_id=?",
            (member_id,)).fetchall()
