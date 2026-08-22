"""Trading-partner throttling, expressed in X12 and not only in HTTP.

THE POINT, AND IT IS NOT THE RATE LIMITER
------------------------------------------
Rate limiting is a solved problem; a token bucket is thirty lines. The part
that is specific to this domain, and that a general-purpose API gateway gets
wrong, is HOW THE REFUSAL IS COMMUNICATED.

A trading partner submitting a 270 has a system that parses 271s. It may not
parse anything else. Returning HTTP 429 with a JSON body to that system is
returning nothing: the response is not a 271, so their parser either errors or
drops it, and the inquiry looks to their operations team like a timeout. They
will retry, because a timeout is retryable, which is precisely the behaviour a
rate limiter exists to prevent.

X12 already has the vocabulary for this. A 271 can carry an AAA rejection with:

    AAA03 = 42   "Unable to respond at current time"
    AAA04 = R    "Resubmission allowed"

That is a rate-limit response in the counterparty's own language. Their system
already knows how to read it, their operations team already has a runbook for
it, and it is retryable in a way their software will understand as deliberate
rather than as a fault.

So the refusal is returned BOTH ways: HTTP 429 with `Retry-After` for anything
speaking HTTP, and a well-formed 271 with AAA 42/R in the body for anything
speaking X12. Neither alone is sufficient.

WHY THE POLICY LIVES IN A TABLE
-------------------------------
The limits below are terms of a TRADING PARTNER AGREEMENT -- a commercial
document -- not engineering constants. The onboarding doc already states them;
this makes the same numbers executable so the two cannot drift, the same
argument as `data1-deid-claims-platform/src/metrics.py`.

That framing decides the behaviour at the limit. The spec's question is what
happens when a partner sends 10,000 inquiries an hour against an agreement for
1,000. The answer is NOT to silently degrade, and not to serve them anyway: it
is to serve the agreed rate, refuse the excess in a form their system
understands, and escalate commercially. Silently absorbing the overage sets a
new de-facto limit that nobody agreed to and that the next partner will also
discover.

WHAT THIS IS NOT
----------------
In-process and single-node: the buckets are a dict, so a second instance
doubles every limit. A real deployment needs shared state (Redis, or a gateway
that owns the counter). No distributed consensus, no burst borrowing across
windows, no per-endpoint limits, no priority lanes for urgent inquiries -- and
that last one is a real gap, because an eligibility check at a bedside is not
the same request as a batch reconciliation.
"""

from __future__ import annotations

import time

# Terms of the trading-partner agreement, per partner ID. `burst` is what the
# bucket holds; `per_hour` is the sustained rate it refills at.
TRADING_PARTNER_AGREEMENTS = {
    "SUBMITTERA": {"per_hour": 1000, "burst": 60,
                   "contact": "edi-ops@partner-a.example",
                   "note": "standard real-time eligibility agreement"},
    "SUBMITTERB": {"per_hour": 200, "burst": 20,
                   "contact": "edi@partner-b.example",
                   "note": "pilot; low volume until certification completes"},
    "BATCHCO":    {"per_hour": 5000, "burst": 500,
                   "contact": "batch-ops@batchco.example",
                   "note": "overnight batch window; high burst by agreement"},
}

DEFAULT_AGREEMENT = {"per_hour": 60, "burst": 10,
                     "contact": "unknown",
                     "note": ("no trading-partner agreement on file. A default "
                              "exists so an unknown submitter is throttled "
                              "rather than either blocked outright or served "
                              "without limit -- both of which have been "
                              "somebody's outage.")}

# AAA 42 / R: 'Unable to respond at current time' / 'Resubmission allowed'.
THROTTLE_AAA_CODE = "42"
THROTTLE_AAA_ACTION = "R"


class Throttle:
    """Token bucket per trading partner, with the agreement as its policy."""

    def __init__(self, agreements=None, clock=None):
        self.agreements = dict(agreements or TRADING_PARTNER_AGREEMENTS)
        self.clock = clock or time.monotonic
        self._buckets = {}
        self.stats = {}

    def agreement(self, partner):
        return self.agreements.get(partner, DEFAULT_AGREEMENT)

    def _bucket(self, partner):
        if partner not in self._buckets:
            a = self.agreement(partner)
            self._buckets[partner] = {"tokens": float(a["burst"]),
                                      "last": self.clock()}
            self.stats.setdefault(partner, {"allowed": 0, "throttled": 0})
        return self._buckets[partner]

    def check(self, partner):
        """Consume one token. Returns (allowed, detail).

        Refill is CONTINUOUS rather than a fixed window. A fixed hourly window
        lets a partner send their whole allowance in the last second of one
        hour and again in the first second of the next -- twice the agreed rate
        across a two-second span, entirely within policy as written. Continuous
        refill is what makes the number in the agreement mean what the
        commercial team thinks it means.
        """
        a = self.agreement(partner)
        b = self._bucket(partner)
        now = self.clock()
        elapsed = max(0.0, now - b["last"])
        b["tokens"] = min(float(a["burst"]),
                          b["tokens"] + elapsed * a["per_hour"] / 3600.0)
        b["last"] = now

        st = self.stats.setdefault(partner, {"allowed": 0, "throttled": 0})
        if b["tokens"] >= 1.0:
            b["tokens"] -= 1.0
            st["allowed"] += 1
            return True, {"partner": partner, "remaining": int(b["tokens"]),
                          "per_hour": a["per_hour"], "burst": a["burst"]}

        st["throttled"] += 1
        deficit = 1.0 - b["tokens"]
        retry_after = max(1, int(deficit * 3600.0 / a["per_hour"]) + 1)
        return False, {
            "partner": partner, "remaining": 0,
            "per_hour": a["per_hour"], "burst": a["burst"],
            "retry_after_seconds": retry_after,
            "aaa_code": THROTTLE_AAA_CODE,
            "aaa_action": THROTTLE_AAA_ACTION,
            "contact": a["contact"],
            "escalation": (
                "Sustained volume above the agreed rate is a commercial "
                "matter, not an engineering one. The correct response is to "
                "serve the agreed rate, refuse the excess in a form the "
                "partner's system understands, and raise it with them -- not "
                "to absorb it, which sets a de-facto limit nobody agreed to."),
        }

    def report(self):
        rows = []
        for partner, st in sorted(self.stats.items()):
            a = self.agreement(partner)
            total = st["allowed"] + st["throttled"]
            rows.append({
                "partner": partner, "agreed_per_hour": a["per_hour"],
                "burst": a["burst"], "allowed": st["allowed"],
                "throttled": st["throttled"], "total": total,
                "throttled_pct": (st["throttled"] / total) if total else 0.0,
                "has_agreement": partner in self.agreements,
                "contact": a["contact"],
            })
        return rows
