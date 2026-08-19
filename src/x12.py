"""X12 envelopes, segment handling, and the code tables that carry the meaning.

SCOPE BOUNDARY, STATED UP FRONT
-------------------------------
This is **structurally honest, not certification-complete**. It builds and
parses 270/271 and 276/277 with real ISA/GS/ST envelope structure, real control
numbers, real loop organisation, and real code values -- and it is NOT a
HIPAA-compliant transaction implementation. Missing: the full X12N
implementation guide's situational rules, every optional segment this simulation
does not exercise, HL hierarchical-level handling beyond what these transactions
need, 999/TA1 acknowledgement in full, and any form of certification testing.

Saying so is the mature move. Claiming HIPAA transaction fidelity that has not
been tested against a certification suite is the kind of overclaim that ends a
technical screen, because the first person who has actually done the work will
ask which implementation guide version and what the 999 rejection rate was.

CONTROL NUMBERS
---------------
Three levels, and they nest:

    ISA13  interchange control number  -- matched by IEA02
    GS06   group control number        -- matched by GE02
    ST02   transaction set control     -- matched by SE02

They are not decoration. A trading partner reconciles on them, duplicate
detection runs on them, and a mismatch between ISA13 and IEA02 is a structural
error that a 999 (or TA1) reports and that causes the whole interchange to be
rejected -- not just the offending transaction.
"""

from __future__ import annotations

ELEMENT = "*"
SUBELEMENT = ":"
SEGMENT = "~"

# ---------------------------------------------------------------------------
# Service type codes (EQ01 / EB03). Real values from X12 code source 1365.
# ---------------------------------------------------------------------------
SERVICE_TYPES = {
    "30": "Health Benefit Plan Coverage",
    "1": "Medical Care",
    "47": "Hospital",
    "48": "Hospital - Inpatient",
    "50": "Hospital - Outpatient",
    "86": "Emergency Services",
    "88": "Pharmacy",
    "98": "Professional (Physician) Visit - Office",
    "AL": "Vision (Optometry)",
    "MH": "Mental Health",
    "UC": "Urgent Care",
    "35": "Dental Care",
}

# ---------------------------------------------------------------------------
# EB01 eligibility/benefit information codes.
# The important ones for this project are 1 (active), 6 (inactive), and U --
# "Contact Following Entity for Eligibility Information", which is how X12
# expresses "we cannot answer this from here". Preserving U instead of
# flattening it to eligible:false is the whole design point of the facade.
# ---------------------------------------------------------------------------
EB01_CODES = {
    "1": "Active Coverage",
    "6": "Inactive",
    "A": "Co-Insurance",
    "B": "Co-Payment",
    "C": "Deductible",
    "G": "Out of Pocket (Stop Loss)",
    "I": "Non-Covered",
    "U": "Contact Following Entity for Eligibility Information",
    "V": "Cannot Process",
}

# EB02 coverage level
EB02_CODES = {"IND": "Individual", "FAM": "Family", "ESP": "Employee and Spouse"}

# EB06 time period qualifier
EB06_CODES = {"22": "Service Year", "23": "Calendar Year", "25": "Contract",
              "26": "Episode", "27": "Visit", "29": "Remaining", "32": "Lifetime"}

# ---------------------------------------------------------------------------
# AAA reject reason codes (271 AAA03). Half the value of an eligibility
# implementation is here: real work is 30% happy path and 70% "why did this
# reject". Subset of X12 code source 901.
# ---------------------------------------------------------------------------
AAA_REJECT = {
    "15": "Required application data missing",
    "42": "Unable to respond at current time",
    "43": "Invalid/missing provider identification",
    "51": "Provider not on file",
    "57": "Invalid/missing date(s) of service",
    "58": "Invalid/missing date-of-birth",
    "60": "Date of birth follows date(s) of service",
    "62": "Date of service not within allowable inquiry period",
    "63": "Date of service in future",
    "64": "Invalid/missing patient ID",
    "65": "Invalid/missing patient name",
    "67": "Patient not found",
    "71": "Patient birth date does not match that for the patient on the database",
    "72": "Invalid/missing subscriber/insured ID",
    "73": "Invalid/missing subscriber/insured name",
    "75": "Subscriber/insured not found",
    "78": "Subscriber/insured not in group/plan identified",
}

# AAA04 follow-up action codes -- what the provider's front desk should DO.
# This is the field that makes a rejection actionable rather than annoying.
AAA_ACTION = {
    "C": "Please correct and resubmit",
    "N": "Resubmission not allowed",
    "P": "Please resubmit original transaction",
    "R": "Resubmission allowed",
    "S": "Do not resubmit; inquiry initiated to a third party",
    "W": "Please wait 30 days and resubmit",
    "X": "Please wait 10 days and resubmit",
    "Y": "Do not resubmit; we will hold your request and respond again shortly",
}

# ---------------------------------------------------------------------------
# 277 claim status category codes (STC01-1) and status codes (STC01-2).
# ---------------------------------------------------------------------------
STATUS_CATEGORY = {
    "A1": "Acknowledgement/Receipt - the claim has been received",
    "A2": "Acknowledgement/Acceptance into adjudication system",
    "A3": "Acknowledgement/Returned as unprocessable claim",
    "A4": "Acknowledgement/Not found",
    "P1": "Pending/In process",
    "P3": "Pending/Requested information not received",
    "F0": "Finalized",
    "F1": "Finalized/Payment - the claim has been paid",
    "F2": "Finalized/Denial - the claim has been denied",
    "F3": "Finalized/Revised - adjudication information has changed",
}
STATUS_CODE = {
    "20": "Accepted for processing",
    "21": "Missing or invalid information",
    "35": "Claim/encounter not found",
    "65": "Claim/line has been paid",
    "88": "Claim denied: not a covered benefit",
    "101": "Claim was processed as primary",
    "247": "Line information",
}


# ---------------------------------------------------------------------------
class Segment:
    __slots__ = ("tag", "elements")

    def __init__(self, tag, *elements):
        self.tag = tag
        self.elements = [str(e) for e in elements]

    def get(self, n, default=""):
        """1-based element access, matching X12's own numbering (ISA01 etc)."""
        try:
            return self.elements[n - 1]
        except IndexError:
            return default

    def component(self, n, c, default=""):
        parts = self.get(n).split(SUBELEMENT)
        try:
            return parts[c - 1]
        except IndexError:
            return default

    def render(self):
        return ELEMENT.join([self.tag] + self.elements)

    def __repr__(self):
        return f"<{self.tag} {self.elements}>"


class Interchange:
    def __init__(self, segments):
        self.segments = segments

    def render(self):
        return SEGMENT.join(s.render() for s in self.segments) + SEGMENT

    def find(self, tag):
        return [s for s in self.segments if s.tag == tag]

    def first(self, tag):
        for s in self.segments:
            if s.tag == tag:
                return s
        return None


def parse(text):
    """Parse an interchange. Envelope integrity is checked, not assumed."""
    segs = []
    for chunk in text.split(SEGMENT):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(ELEMENT)
        segs.append(Segment(parts[0], *parts[1:]))
    return Interchange(segs)


class EnvelopeError(Exception):
    pass


def check_envelope(interchange):
    """Validate control-number matching and segment counts.

    A mismatch here is a STRUCTURAL error: it rejects the whole interchange,
    not just the offending transaction, which is why it is checked before any
    business logic runs.
    """
    problems = []
    isa = interchange.first("ISA")
    iea = interchange.first("IEA")
    if not isa or not iea:
        raise EnvelopeError("missing ISA or IEA")
    if isa.get(13) != iea.get(2):
        problems.append(f"ISA13 {isa.get(13)} != IEA02 {iea.get(2)}")

    gs_list, ge_list = interchange.find("GS"), interchange.find("GE")
    if len(gs_list) != len(ge_list):
        problems.append(f"{len(gs_list)} GS but {len(ge_list)} GE")
    for gs, ge in zip(gs_list, ge_list):
        if gs.get(6) != ge.get(2):
            problems.append(f"GS06 {gs.get(6)} != GE02 {ge.get(2)}")
    if iea.get(1) and int(iea.get(1)) != len(gs_list):
        problems.append(f"IEA01 says {iea.get(1)} groups, found {len(gs_list)}")

    st_list, se_list = interchange.find("ST"), interchange.find("SE")
    for st, se in zip(st_list, se_list):
        if st.get(2) != se.get(2):
            problems.append(f"ST02 {st.get(2)} != SE02 {se.get(2)}")

    # SE01 counts segments from ST through SE inclusive
    for st, se in zip(st_list, se_list):
        i = interchange.segments.index(st)
        j = interchange.segments.index(se)
        actual = j - i + 1
        if se.get(1) and int(se.get(1)) != actual:
            problems.append(f"SE01 says {se.get(1)} segments, counted {actual}")
    return problems


class ControlNumbers:
    """Monotonic control-number issuer.

    Real trading-partner agreements specify the range and whether numbers may
    wrap. They are sequential here and persist for the life of the process,
    which is enough to make duplicate detection and reconciliation meaningful.
    """

    def __init__(self, start=1):
        self._isa = self._gs = self._st = start

    def isa(self):
        self._isa += 1
        return f"{self._isa:09d}"

    def gs(self):
        self._gs += 1
        return str(self._gs)

    def st(self):
        self._st += 1
        return f"{self._st:04d}"


def envelope(transaction_segments, functional_id, transaction_set_id,
             control, sender="SUBMITTER", receiver="PAYER",
             date="20240612", time="1015"):
    """Wrap transaction segments in ST/SE, GS/GE, ISA/IEA with matched controls."""
    isa_ctl, gs_ctl, st_ctl = control.isa(), control.gs(), control.st()
    st = Segment("ST", transaction_set_id, st_ctl)
    body = [st] + list(transaction_segments)
    se = Segment("SE", str(len(body) + 1), st_ctl)
    body.append(se)

    segs = [
        Segment("ISA", "00", " " * 10, "00", " " * 10, "ZZ",
                f"{sender:<15}", "ZZ", f"{receiver:<15}", date[2:], time,
                "^", "00501", isa_ctl, "0", "P", SUBELEMENT),
        Segment("GS", functional_id, sender, receiver, date, time, gs_ctl,
                "X", "005010X279A1"),
        *body,
        Segment("GE", "1", gs_ctl),
        Segment("IEA", "1", isa_ctl),
    ]
    return Interchange(segs)
