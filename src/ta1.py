"""TA1 interchange acknowledgement, and priority lanes.

TWO NAMED GAPS
--------------
"**No TA1** (interchange acknowledgement) at all."
"no priority lanes -- an eligibility check at a bedside is not the same request
as a batch reconciliation, and this throttles them identically."

WHY TA1 IS NOT JUST ANOTHER ACK
--------------------------------
X12 has three acknowledgement levels and they answer different questions at
different layers:

    TA1   the INTERCHANGE (ISA/IEA) envelope. Did the outer wrapper parse, do
          the control numbers match, is the segment terminator what ISA16 says?
    999   the FUNCTIONAL GROUP and transaction sets inside it. Are the segments
          in the right order, are required elements present, are code values
          valid?
    271   the BUSINESS answer. Is this member eligible?

`ack999.py` covers the second. The first was missing entirely, and its absence
has a specific consequence: WHEN THE ENVELOPE IS BROKEN THERE IS NOTHING TO
ACKNOWLEDGE AT THE 999 LEVEL. A 999 is itself wrapped in an ISA/GS envelope and
references the GS control number of what it acknowledges -- if the interchange
did not parse, that reference cannot be constructed. Returning a 999 for a
malformed ISA is not merely wrong, it is not expressible.

The serve.py facade previously returned HTTP 400 for that case and said in its
response that a real implementation answers with TA1. This is the TA1.

TA104 IS THE PART THAT MATTERS
-------------------------------
The acknowledgement code says what the sender should do next, and the three
values are not interchangeable:

    A   accepted
    E   accepted WITH errors -- the interchange is usable, note the problem
    R   REJECTED -- the interchange was not processed at all

`E` on something unprocessable is the dangerous one: the sender's system reads
"accepted", marks the batch delivered, and nobody discovers the claims never
arrived until reconciliation weeks later. So `build_ta1` derives the code from
the error rather than accepting it as a parameter, and every structural error
maps to `R`.

PRIORITY LANES
--------------
An eligibility check at a bedside and an overnight batch reconciliation are the
same transaction type and are not the same request. Throttling them identically
means the batch consumes the bucket and the bedside check is refused -- which is
the wrong outcome in the only case that matters.

`Lane` splits each partner's allowance so interactive traffic cannot be starved
by batch traffic, and the reservation is one-directional: INTERACTIVE MAY BORROW
FROM THE BATCH ALLOWANCE, BATCH MAY NOT BORROW FROM INTERACTIVE. A reserved
share that both lanes can spend is not a reservation.

WHAT THIS IS NOT
----------------
TA1 only, not TA3. No AK9 partial-acceptance subtleties, no ISA13 duplicate
detection across sessions (the control-number history would have to be
persistent), and no per-endpoint or per-transaction-type lane beyond the two
here.
"""

from __future__ import annotations

import re

# TA105 interchange note codes, 004010/005010.
TA1_NOTE = {
    "000": "No error",
    "001": "The Interchange Control Number in the Header and Trailer Do Not Match",
    "002": "This Standard as Noted in the Control Standards Identifier is Not Supported",
    "003": "This Version of the Controls is Not Supported",
    "004": "The Segment Terminator is Invalid",
    "005": "Invalid Interchange ID Qualifier for Sender",
    "006": "Invalid Interchange Sender ID",
    "007": "Invalid Interchange ID Qualifier for Receiver",
    "008": "Invalid Interchange Receiver ID",
    "009": "Unknown Interchange Receiver ID",
    "010": "Invalid Authorization Information Qualifier Value",
    "013": "Invalid Security Information Qualifier Value",
    "016": "Invalid Number of Included Groups Value",
    "017": "Invalid Control Structure",
    "018": "Invalid Interchange Date Value",
    "019": "Invalid Interchange Time Value",
    "021": "Invalid Number of Included Groups Value",
    "022": "Invalid Control Structure",
    "023": "Improper (Premature) End-of-File (Transmission)",
    "024": "Invalid Interchange Content",
    "025": "Duplicate Interchange Control Number",
}

# TA104 acknowledgement codes.
TA1_ACK = {"A": "Accepted",
           "E": "Accepted, but errors were noted",
           "R": "Rejected"}

# Every structural error rejects. See the module docstring: 'E' on something
# unprocessable is how a sender marks a batch delivered that never arrived.
REJECTING = set(TA1_NOTE) - {"000"}


def inspect_interchange(raw):
    """Structural checks on the ISA/IEA envelope only. Returns TA105 codes.

    Deliberately does NOT look inside the functional group. That is the 999's
    job, and a TA1 that reports transaction-level problems is answering at the
    wrong layer -- the sender routes TA1s to a different queue from 999s
    precisely because the two mean different things.
    """
    errors = []
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

    if not text.startswith("ISA"):
        return ["024"], {}                     # invalid interchange content
    if len(text) < 106:
        return ["023"], {}                     # premature end of file

    # ISA is fixed-width: the element separator is ISA[3], and ISA16 (the
    # component separator) is at position 104, with the segment terminator
    # immediately after.
    sep = text[3]
    terminator = text[105] if len(text) > 105 else None
    isa = text[:106].split(sep)
    if len(isa) < 17:
        return ["017"], {}                     # invalid control structure

    meta = {"sender_qualifier": isa[5].strip(), "sender_id": isa[6].strip(),
            "receiver_qualifier": isa[7].strip(),
            "receiver_id": isa[8].strip(), "date": isa[9].strip(),
            "time": isa[10].strip(), "version": isa[12].strip(),
            "control_number": isa[13].strip(), "terminator": terminator}

    if isa[1].strip() not in ("00", "03"):
        errors.append("010")
    if isa[3].strip() not in ("00", "01"):
        errors.append("013")
    if meta["sender_qualifier"] not in ("01", "14", "20", "27", "28", "29",
                                        "30", "33", "ZZ"):
        errors.append("005")
    if not meta["sender_id"]:
        errors.append("006")
    if meta["receiver_qualifier"] not in ("01", "14", "20", "27", "28", "29",
                                          "30", "33", "ZZ"):
        errors.append("007")
    if not meta["receiver_id"]:
        errors.append("008")
    if not re.fullmatch(r"\d{6}", meta["date"]):
        errors.append("018")
    if not re.fullmatch(r"\d{4}", meta["time"]):
        errors.append("019")
    if meta["version"] not in ("00401", "00501"):
        errors.append("003")
    if not re.fullmatch(r"\d{9}", meta["control_number"]):
        errors.append("001")

    # IEA must exist and its control number must match ISA13.
    # NOT r"IEA\%s..." -- with sep="*" that builds `IEA\*(\d+)`, which reads
    # as "IEA then zero or more backslashes" and never matches a real IEA. The
    # symptom was every valid interchange rejected with note 023 (premature
    # end of file), which is the most misleading possible answer: it tells the
    # sender their transmission was truncated when it arrived intact.
    esc = re.escape(sep)
    iea = re.search("IEA" + esc + r"(\d+)" + esc + r"(\d+)", text) if sep else None
    if not iea:
        errors.append("023")
    else:
        n_groups, iea_ctl = iea.group(1), iea.group(2)
        if iea_ctl.lstrip("0") != meta["control_number"].lstrip("0"):
            errors.append("001")
        actual = text.count(f"GS{sep}")
        if int(n_groups) != actual:
            errors.append("021")
    return errors, meta


def build_ta1(raw, seen_control_numbers=(), sep="*", terminator="~"):
    """A TA1 segment plus the interchange it belongs in.

    TA104 IS DERIVED, NOT PASSED IN. Letting a caller choose the
    acknowledgement code is how 'E' ends up on an unprocessable interchange,
    after which the sender's system marks the batch delivered and nobody finds
    out until reconciliation.
    """
    errors, meta = inspect_interchange(raw)
    ctl = meta.get("control_number") or "000000000"

    if ctl and ctl.lstrip("0") in {c.lstrip("0")
                                   for c in seen_control_numbers}:
        errors = list(errors) + ["025"]        # duplicate interchange

    code = "R" if any(e in REJECTING for e in errors) else "A"
    note = errors[0] if errors else "000"

    ta1 = sep.join(["TA1", ctl, meta.get("date", "") or "000000",
                    meta.get("time", "") or "0000", code, note]) + terminator
    return {
        "segment": ta1,
        "interchange_control_number": ctl,
        "ack_code": code, "ack_meaning": TA1_ACK[code],
        "note_code": note, "note": TA1_NOTE.get(note, "unknown"),
        "all_errors": [{"code": e, "note": TA1_NOTE.get(e, "unknown")}
                       for e in errors],
        "layer": ("interchange envelope (ISA/IEA). A 999 cannot answer here: "
                  "a 999 is itself wrapped in an envelope and references the "
                  "GS control number of what it acknowledges, so when the "
                  "interchange does not parse that reference is not "
                  "constructible."),
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# priority lanes
# ---------------------------------------------------------------------------

INTERACTIVE, BATCH = "interactive", "batch"

# Fraction of a partner's allowance reserved for interactive traffic.
INTERACTIVE_RESERVE = 0.30


class Lane:
    """Split a partner's allowance so batch cannot starve interactive.

    THE RESERVATION IS ONE-DIRECTIONAL. Interactive may borrow from the batch
    share when batch is idle; batch may NOT borrow from the interactive
    reserve. A reserved share that both lanes can spend is not a reservation --
    it is a suggestion, and the overnight batch will spend it every night.

    The classification is by declared lane rather than inferred from volume: a
    submitter sending 500 inquiries in a second is probably batch, but so is a
    hospital admitting a bus crash, and guessing wrong there refuses the
    request that mattered.
    """

    def __init__(self, per_hour, burst, reserve=INTERACTIVE_RESERVE,
                 clock=None):
        import time as _t
        self.per_hour, self.burst = per_hour, burst
        self.reserve = reserve
        self.clock = clock or _t.monotonic
        now = self.clock()
        self.state = {
            INTERACTIVE: {"tokens": burst * reserve, "last": now,
                          "rate": per_hour * reserve,
                          "cap": burst * reserve},
            BATCH: {"tokens": burst * (1 - reserve), "last": now,
                    "rate": per_hour * (1 - reserve),
                    "cap": burst * (1 - reserve)},
        }

    def _refill(self, lane):
        st = self.state[lane]
        now = self.clock()
        st["tokens"] = min(st["cap"],
                           st["tokens"] + (now - st["last"]) * st["rate"] / 3600)
        st["last"] = now
        return st

    def check(self, lane=INTERACTIVE):
        if lane not in (INTERACTIVE, BATCH):
            raise ValueError(f"lane must be {INTERACTIVE!r} or {BATCH!r}")
        st = self._refill(lane)
        if st["tokens"] >= 1.0:
            st["tokens"] -= 1.0
            return True, {"lane": lane, "borrowed": False,
                          "remaining": int(st["tokens"])}

        if lane == INTERACTIVE:
            # ONE-DIRECTIONAL BORROWING. Interactive may take from batch.
            other = self._refill(BATCH)
            if other["tokens"] >= 1.0:
                other["tokens"] -= 1.0
                return True, {"lane": lane, "borrowed": True,
                              "borrowed_from": BATCH,
                              "why": ("the interactive reserve was exhausted "
                                      "and batch capacity was idle. Batch "
                                      "cannot borrow the other way -- a "
                                      "reserve both lanes can spend is not a "
                                      "reserve.")}
        return False, {"lane": lane, "borrowed": False, "remaining": 0,
                       "why": (f"{lane} allowance exhausted"
                               + ("" if lane == INTERACTIVE else
                                  ". Batch may not borrow from the "
                                  "interactive reserve: an eligibility check "
                                  "at a bedside must not be refused because "
                                  "an overnight reconciliation spent the "
                                  "bucket."))}

    def report(self):
        return {lane: {"tokens": round(self._refill(lane)["tokens"], 2),
                       "cap": self.state[lane]["cap"],
                       "rate_per_hour": self.state[lane]["rate"]}
                for lane in (INTERACTIVE, BATCH)}
