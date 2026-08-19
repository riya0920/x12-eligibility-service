"""999 Implementation Acknowledgement: syntax errors, reported the X12 way.

WHY A SEPARATE ACKNOWLEDGEMENT EXISTS AT ALL
--------------------------------------------
A 271 answers a business question ("is this member covered"). A 999 answers a
completely different one: **"was your transaction syntactically usable"**. They
are separate because the failures are separate, and conflating them is the most
common mistake a new EDI integration makes.

A member who does not exist is a BUSINESS outcome and comes back as a 271 with
an AAA segment. A missing required element is a SYNTAX outcome, and there is no
271 at all -- the transaction never reached adjudication. A trading partner
that only implements the 271 path cannot tell "we processed your inquiry and the
answer is no" from "we could not read your file", and those need completely
different responses from completely different teams.

THE THREE LEVELS, AND WHY THEY NEST
-----------------------------------
    AK1/AK9   the FUNCTIONAL GROUP -- how many transaction sets were received,
              accepted, rejected. A group-level failure rejects everything in
              it, which is why control-number integrity is checked first.
    AK2/IK5   one TRANSACTION SET -- accepted, accepted-with-errors, rejected.
    IK3/IK4   one SEGMENT and one ELEMENT inside it, with a position so the
              sender can find it.

The position fields are the whole point of IK3/IK4. "Your file was rejected" is
useless to the person who has to fix it; "segment 7, element 3, invalid code
value" is a ticket someone can close.

SCOPE, STATED
-------------
This validates the structural rules this codebase can actually check --
envelope integrity, required segments, required elements, and code values
against the tables in `x12.py`. It is NOT a full 005010X231 implementation
acknowledgement: no implementation-guide-specific situational rules, no loop
repetition limits, no CTX segments for business-unit context. The distinction
matters because a real trading partner tests against a certification suite, and
claiming coverage that has not been tested that way is the overclaim.
"""

from __future__ import annotations

import x12
from x12 import Segment

# IK3 segment error codes (X12 code source 618, subset)
IK3_ERRORS = {
    "1": "Unrecognised segment ID",
    "2": "Unexpected segment",
    "3": "Required segment missing",
    "4": "Loop occurs over maximum times",
    "5": "Segment exceeds maximum use",
    "8": "Segment has data element errors",
}

# IK4 element error codes (X12 code source 725, subset)
IK4_ERRORS = {
    "1": "Required data element missing",
    "2": "Conditional required data element missing",
    "4": "Invalid character in data element",
    "5": "Data element too short",
    "6": "Data element too long",
    "7": "Invalid code value",
}

# IK5 / AK9 acknowledgement codes
ACK_CODES = {
    "A": "Accepted",
    "E": "Accepted but errors were noted",
    "R": "Rejected",
}

# Segments each transaction set must carry, and the elements each must fill.
REQUIRED = {
    "270": {
        "segments": ["BHT", "HL", "NM1", "EQ"],
        "elements": {"BHT": [1, 2], "EQ": [1]},
    },
    "276": {
        "segments": ["BHT", "HL", "NM1", "TRN"],
        "elements": {"BHT": [1, 2], "TRN": [1]},
    },
}

# Elements whose values must come from a known code table.
CODED_ELEMENTS = {
    ("EQ", 1): x12.SERVICE_TYPES,
}


class Error:
    __slots__ = ("segment", "position", "element", "code", "kind", "detail",
                 "envelope")

    def __init__(self, segment, position, code, kind, element=None, detail="",
                 envelope=False):
        self.segment = segment
        self.position = position
        self.element = element
        self.code = code
        self.kind = kind          # "segment" or "element"
        self.detail = detail
        # Envelope errors are categorically different: they invalidate the
        # whole interchange rather than one transaction set, so they force a
        # rejection regardless of what else parsed cleanly.
        self.envelope = envelope

    def describe(self):
        table = IK3_ERRORS if self.kind == "segment" else IK4_ERRORS
        where = f"segment {self.position} ({self.segment})"
        if self.element is not None:
            where += f", element {self.element}"
        return f"{where}: {self.code} {table.get(self.code, '?')} {self.detail}"


def validate(interchange):
    """Return (errors, transaction_set_id). Structural checks only."""
    errors = []

    envelope_problems = []
    try:
        envelope_problems = x12.check_envelope(interchange)
    except x12.EnvelopeError as exc:
        errors.append(Error("ISA", 1, "3", "segment", detail=str(exc)))
        return errors, None

    st = interchange.first("ST")
    ts_id = st.get(1) if st else None
    if ts_id is None:
        errors.append(Error("ST", 1, "3", "segment",
                            detail="no transaction set found"))
        return errors, None

    for p in envelope_problems:
        errors.append(Error("GE" if "GS06" in p else "IEA", 1, "8", "segment",
                            detail=p, envelope=True))

    spec = REQUIRED.get(ts_id)
    if spec is None:
        return errors, ts_id

    present = {s.tag for s in interchange.segments}
    for tag in spec["segments"]:
        if tag not in present:
            errors.append(Error(tag, 0, "3", "segment",
                                detail="required by the implementation guide"))

    for pos, seg in enumerate(interchange.segments, start=1):
        for idx in spec["elements"].get(seg.tag, []):
            if not seg.get(idx):
                errors.append(Error(seg.tag, pos, "1", "element", element=idx,
                                    detail="is required"))
        for (tag, idx), table in CODED_ELEMENTS.items():
            if seg.tag == tag:
                value = seg.get(idx)
                if value and value not in table:
                    errors.append(Error(seg.tag, pos, "7", "element",
                                        element=idx,
                                        detail=f"value {value!r} not in the code list"))
    return errors, ts_id


def build_999(interchange, control, errors=None, ts_id=None):
    """Build a 999 for an interchange. Returns (interchange, summary)."""
    if errors is None:
        errors, ts_id = validate(interchange)

    st = interchange.first("ST")
    gs = interchange.first("GS")
    group_control = gs.get(6) if gs else "1"
    ts_control = st.get(2) if st else "0001"

    if not errors:
        ack, note = "A", "accepted"
    elif any(e.envelope for e in errors):
        # A control-number or segment-count mismatch means the interchange
        # itself cannot be trusted. Accepting the transactions inside a broken
        # envelope is how a partial file gets processed as though it were whole.
        ack, note = "R", "rejected: envelope integrity failure"
    elif any(e.code == "3" and e.kind == "segment" for e in errors):
        ack, note = "R", "rejected: a required segment is missing"
    else:
        ack, note = "E", "accepted with errors noted"

    segs = [
        Segment("AK1", "HS" if ts_id == "270" else "HR", group_control,
                "005010X279A1"),
        Segment("AK2", ts_id or "270", ts_control, "005010X279A1"),
    ]
    for e in errors:
        if e.kind == "segment":
            segs.append(Segment("IK3", e.segment, str(e.position), "", e.code))
        else:
            segs.append(Segment("IK3", e.segment, str(e.position), "", "8"))
            segs.append(Segment("IK4", str(e.element), "", e.code))
    segs.append(Segment("IK5", ack))
    segs.append(Segment("AK9", ack, "1", "1", "1" if ack != "R" else "0"))

    out = x12.envelope(segs, "FA", "999", control)
    return out, {"ack": ack, "ack_meaning": ACK_CODES[ack], "note": note,
                 "n_errors": len(errors),
                 "errors": [e.describe() for e in errors],
                 "transaction_set": ts_id}


def parse_999(interchange):
    """Read a 999 back. Used to prove the round trip and to drive the
    trading-partner error dashboard."""
    ak9 = interchange.first("AK9")
    ik5 = interchange.first("IK5")
    errors = []
    ik3s = interchange.find("IK3")
    ik4s = interchange.find("IK4")
    for seg in ik3s:
        errors.append({"segment": seg.get(1), "position": seg.get(2),
                       "code": seg.get(4),
                       "meaning": IK3_ERRORS.get(seg.get(4), "?")})
    for seg in ik4s:
        errors.append({"element": seg.get(1), "code": seg.get(3),
                       "meaning": IK4_ERRORS.get(seg.get(3), "?")})
    ack = ik5.get(1) if ik5 else (ak9.get(1) if ak9 else "?")
    return {"ack": ack, "ack_meaning": ACK_CODES.get(ack, "?"),
            "n_included": int(ak9.get(2)) if ak9 and ak9.get(2) else 0,
            "n_accepted": int(ak9.get(4)) if ak9 and ak9.get(4) else 0,
            "errors": errors}
