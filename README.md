# SE-2 — Eligibility & claim status (X12 EDI facade) — working system, 9 known gaps

270/271 and 276/277 with real envelopes, real control numbers, and real AAA
rejection semantics — behind a JSON facade that **refuses to flatten an
ambiguous answer into a boolean**.

```bash
python run_demo.py         # real-time + batch, accumulators, 999, round-trip
python serve.py --demo     # the HTTP facade, both languages, the throttle
python serve.py            # serve on :8088; /dashboard.html
python -m pytest tests -q  # 78 tests
```

Offline, under a second, standard library only.

## Scope boundary, stated first

This is **structurally honest, not certification-complete**. Real envelopes,
real loops, real code values, and a 999 that reports genuine syntax errors with
positions — and *not* a HIPAA-compliant transaction implementation. Missing: the
X12N implementation guide's situational rules, TA1 interchange acknowledgement,
CTX context segments, and any certification testing.

Saying so is the mature move. Claiming transaction fidelity that has not been
tested against a certification suite is the overclaim that ends a screen,
because the first person who has actually done this work asks which guide
version and what the 999 rejection rate was.

---

## The five things worth reading

### 1. Eligibility is a question with five answers, not two

The design point the whole project hangs on. A facade returning
`{"eligible": true}` has thrown away the part that matters.

| state | X12 | JSON `benefit_determination` | what the front desk does |
|---|---|---|---|
| active, benefit known | `EB01=1` + amounts | `active_coverage_benefits_known` | collect the copay |
| active, **not administered here** | `EB01=U` | `unknown_contact_other_entity` | call the carve-out administrator |
| active, service not covered | `EB01=I` | `service_not_covered` | member pays, or appeal |
| coverage inactive on the DOS | `EB01=6` + `DTP*347` | `coverage_inactive_on_date_of_service` | check for other coverage |
| inquiry rejected | `AAA`, **no EB at all** | `inquiry_rejected` | correct and resubmit |

**Rows two and three are the ones a boolean destroys.** "Vision is not covered"
and "the dental administrator handles this" are both not-a-yes, and collapsing
them is not a cosmetic loss: a front desk told *not covered* for a carve-out
benefit **denies care the member actually has**. So the response carries
`"note": "This is NOT a denial and must not be presented to the member as one."`
and names the entity to call.

`benefit_determination` is a string. There is no `eligible` key anywhere, and a
test asserts that.

### 2. AAA rejection semantics — the 70% of the job

17 AAA reject reason codes implemented, each with an **AAA04 follow-up action**,
because a rejection without an action is annoying rather than useful.

The distinction that matters:

| situation | AAA03 | meaning |
|---|---|---|
| no such member | **75** | Subscriber/insured not found — *check the ID* |
| member exists, DOB wrong | **71** | Birth date does not match — *check the DOB* |

A service that returns "not eligible" for both sends the patient away in the
second case, when the correct action is to re-key one field. That is the entire
argument for modelling rejections properly, and
`test_dob_mismatch_rejects_with_aaa_71_not_75` pins it.

A rejected 271 contains **no EB segments at all** — otherwise a receiver could
read benefits out of a failed inquiry.

### 3. Accumulators link eligibility to the claims store

```
before claim:  deductible remaining $1,500.00   OOP remaining $6,000.00
adjudicate CLM0001: billed $4,200 -> allowed $2,604, plan paid $883.20,
                    member owes $1,720.80  ($1,500.00 to deductible)
after claim:   deductible remaining $0.00       OOP remaining $4,279.20
```

The 271 moved because a claim adjudicated between the two inquiries. **That
linkage is what makes this a system rather than two toys sitting next to each
other.** The out-of-pocket maximum genuinely caps member responsibility —
`test_out_of_pocket_maximum_caps_member_responsibility` runs twelve claims
through and asserts the member is never charged past the cap.

**The consistency question, answered explicitly.** Reads here are
read-your-writes because both live in one SQLite database. Real payers usually
are **not** — adjudication runs on a separate cycle, so a 271 issued ten seconds
after a claim commonly reflects the *pre-claim* accumulator. That is a property
of the architecture, not a bug in the eligibility service, and it is why every
answered response here carries:

> Accumulators reflect claims adjudicated at the time of this inquiry. Claims in
> flight are not included, so remaining deductible and out-of-pocket are
> point-in-time estimates and must not be quoted to a member as a final amount.

Quoting a patient an exact remaining deductible from a 271 is where provider
billing offices get into trouble.

### 4. Envelope integrity, round-trip fidelity, and batch

**Control numbers nest and are checked before any business logic runs:**

```
well-formed        : no problems
IEA02 tampered     : ['ISA13 000000036 != IEA02 99999000000036']
SE01 count wrong   : ['ST02 0072 != SE02 13', 'SE01 says 99 segments, counted 13']
```

This is 999/TA1 territory — a control-number mismatch rejects **the whole
interchange**, not just the offending transaction.

**Round-trip fidelity: 6/6** transactions survive generate → serialise → parse
with identical semantics.

**Batch mode** watches a directory, processes `270_batch_*.edi` into
`271_batch_*.edi`, and reports **control totals** (5 in, 5 out, BALANCED) and a
rejection rate. 1,636 transactions/sec single-core against an in-memory database
— a correctness demo, not a benchmark.

*Why payers still run batch in 2026:* volume economics (a nightly file of
400,000 inquiries costs a fraction of 400,000 real-time calls), legacy trading
partners whose systems only speak files, and overnight refresh being genuinely
sufficient for tomorrow's scheduled appointments. It is a different workload,
not technical debt.

### 5. The 999, and the distinction new integrations get wrong

A **271 answers a business question** — is this member covered. A **999 answers
a completely different one** — was your transaction syntactically usable.

| question | answered by | example |
|---|---|---|
| Was my file usable? | **999** | required segment missing → `IK5*R` |
| What is the business answer? | **271** | member not found → `AAA*Y**75*C` |

A member who does not exist is a *business* outcome. A missing required element
is a *syntax* outcome and **there is no 271 at all** — the transaction never
reached adjudication. A partner that only implements the 271 path cannot tell
*"we processed your inquiry and the answer is no"* from *"we could not read your
file"*, and those need different responses from different teams.
`test_syntax_and_business_failures_are_different_channels` pins the distinction.

| scenario | IK5 | outcome |
|---|---|---|
| well-formed 270 | `A` | accepted |
| invalid service-type code | `E` | accepted with errors noted |
| required EQ segment missing | `R` | rejected |
| tampered interchange control number | `R` | rejected: envelope integrity failure |

**IK3 and IK4 carry the segment position and element number**, which is the
whole point:

```
segment 14 (EQ), element 1: 7 Invalid code value value 'ZZ' not in the code list
```

*"Your file was rejected"* is useless to the person who has to fix it. That line
is a ticket they can close.

**An envelope failure rejects the whole interchange**, not one transaction,
because accepting the transactions inside a broken envelope is how a partial
file gets processed as though it were whole.

### Plus: the trading-partner onboarding document

[`docs/TRADING_PARTNER_ONBOARDING.md`](docs/TRADING_PARTNER_ONBOARDING.md) — the
artefact an integration team actually maintains, written as a companion to a
trading partner agreement rather than a substitute for one. Identifiers, what we
support and explicitly do not, the 999-vs-271 distinction, how to read a 271
without collapsing it to a boolean, batch reconciliation, a test-to-production
promotion sequence whose **step 3 is deliberately the rejection paths** (where
most integrations are actually found to be broken, because the happy path works
early and nobody exercises the other 70% until go-live), and a
symptom→first-thing-to-check table.

It also answers the volume question the spec poses — *10K inquiries/hour for
members we mostly do not cover* — by putting it where it belongs: **the TPA
governs it**. Serve within the agreed rate, respond `AAA*42` (unable to respond,
wait and resubmit) beyond it, and escalate persistent overage commercially
rather than degrading silently, because silent degradation is the worst option —
the partner cannot tell it is happening and will diagnose it as their own
problem.

---

## Throttling that speaks X12, an HTTP facade, and a dashboard

Closes three named gaps: no HTTP layer, no throttling enforcement, no
transaction dashboard.

### The rate limiter is thirty lines. The refusal is the hard part.

A trading partner submitting a 270 has a system that parses 271s. It may parse
nothing else. **Returning HTTP 429 with a JSON body to that system is returning
nothing**: the response is not a 271, so their parser errors or drops it, and
the inquiry looks to their operations team like a *timeout*. They will retry,
because timeouts are retryable — which is precisely the behaviour a rate
limiter exists to prevent.

X12 already has the vocabulary:

```
AAA03 = 42   Unable to respond at current time
AAA04 = R    Resubmission allowed
```

So the refusal goes back **both ways** — HTTP 429 with `Retry-After` for
anything speaking HTTP, and a well-formed 271 carrying `AAA*Y**42*R` for
anything speaking X12. Neither alone is sufficient:

```
request 4: 429  Retry-After 60s  AAA 42 'Unable to respond at current time' / Resubmission allowed

the same refusal to an X12 client -> 429 application/edi-x12
  X-AAA-Code: 42   Retry-After: 60
  and in the body: AAA*Y**42*R
```

A throttled 271 carries **no EB segments at all** — a throttled inquiry has no
benefit answer, and returning EB with unknown values would be worse than
refusing.

### The limits are contract terms, not engineering constants

`TRADING_PARTNER_AGREEMENTS` makes the onboarding document's numbers
executable, so the two cannot drift. That framing settles the spec's question —
what happens when a partner sends 10,000 inquiries an hour against an agreement
for 1,000:

> Serve the agreed rate, refuse the excess in a form their system understands,
> and escalate commercially. **Absorbing it silently sets a de-facto limit
> nobody agreed to**, and the next partner discovers it too.

Refill is **continuous**, not a fixed window. A fixed hourly window lets a
partner send their whole allowance in the last second of one hour and again in
the first second of the next — twice the agreed rate across two seconds,
entirely within policy as written.

An unknown submitter gets a **default agreement** rather than being blocked
outright or served without limit. Both of those have been somebody's outage.

### The dashboard reports rejections by AAA code, not as one rate

| AAA03 | reason | n | share |
|---|---|---|---|
| `42` | Unable to respond at current time | 4 | 36.4% |
| `75` | Subscriber/insured not found | 1 | 9.1% |

`75` is the submitter sending bad member IDs. `42` is **us** throttling them.
`57` would be their date formatting. Three different phone calls — and a single
rejection-rate figure conflates all of them and tells an operations team
nothing about who to ring.

### Two facades, and the source document travels with the translation

`POST /x12/270` speaks raw X12. `POST /eligibility` speaks JSON, for a front
end that should never have to know what an EB01 code is — but it returns the
**271 alongside**, because a facade that discards the source document makes
every disagreement between two systems unresolvable. When a provider says *"your
system told us the patient was covered"*, the answer has to be the 271 that was
actually sent.

An unknown member is **HTTP 200 with `answered: false`** and AAA 75, not a 404.
The transaction succeeded; the *answer* is a rejection. A 404 conflates "we
could not process your inquiry" with "this member does not exist", and the front
desk needs the second one to know to correct the ID and resubmit.

### A bug the tests found

`x12.parse` is tolerant by design and returns an `Interchange` for anything. A
payload of `"this is not X12"` therefore parsed to an empty inquiry, reached
`build_271`, and came back **200 with AAA 75 "subscriber not found"** — an
answer about a member, to something that was never a transaction. The facade now
validates the envelope first. In X12 terms a malformed interchange deserves a
**TA1** interchange acknowledgement rather than any 271; TA1 is not implemented
(it is on the gap list), so this returns 400 and says so rather than pretending
the envelope was fine.

## TA1 — the acknowledgement a 999 cannot give

`src/ta1.py`. The previous gap list said "**No TA1** (interchange
acknowledgement) at all", and the facade returned HTTP 400 for a malformed
envelope while admitting in its own response body that a real implementation
answers with TA1.

X12 acknowledges at three layers and they are not interchangeable:

| level | scope | question |
|---|---|---|
| **TA1** | ISA/IEA envelope | did the outer wrapper parse? |
| 999 | functional group | are the segments and elements valid? |
| 271 | business | is this member eligible? |

**A 999 cannot answer at the interchange level.** A 999 is itself wrapped in an
envelope and references the GS control number of what it acknowledges — when the
ISA does not parse, that reference is not constructible. Returning a 999 for a
malformed interchange is not merely wrong; it is not expressible.

```
good envelope   -> A 000  No error
truncated       -> R 023  Improper (Premature) End-of-File
garbage         -> R 024  Invalid Interchange Content
replayed        -> R 025  Duplicate Interchange Control Number
```

**TA104 is derived, never passed in.** `E` — *accepted with errors* — on
something unprocessable is how a sender's system marks a batch delivered that
never arrived, and nobody finds out until reconciliation weeks later. Letting a
caller choose the code is how that happens, so `build_ta1` takes no such
parameter and every structural error maps to `R`.

## Priority lanes — a bedside check is not a batch job

An eligibility check at a bedside and an overnight reconciliation are the same
*transaction type* and are not the same *request*. Throttling them identically
means the batch spends the bucket and the bedside check is refused, which is the
wrong outcome in the only case that matters.

`Lane` reserves 30% of each partner's allowance for interactive traffic, and the
reservation is **one-directional**: interactive may borrow idle batch capacity;
**batch may never borrow the interactive reserve**. A reserved share both lanes
can spend is not a reserve — it is a suggestion, and the overnight batch will
spend it every night.

The lane is **declared, not inferred**. A submitter sending 500 inquiries in a
second is probably batch — but so is a hospital admitting a bus crash, and
guessing wrong there refuses the request that mattered.

## Three bugs found while building this

- **A regex that rejected every valid interchange.** `r"IEA\%s"` with
  `re.escape("*")` builds `IEA\\*(\d+)`, which reads as *"IEA followed by zero
  or more backslashes"* and never matches. Every well-formed transmission came
  back `R 023 — premature end of file`, which is the most misleading answer
  available: it tells the sender their transmission was truncated when it
  arrived intact.
- **Duplicate detection was global, not per sender.** ISA13 is unique *within
  one sender's* interchanges, and two trading partners both numbering from 1 is
  normal. A global set rejected partner B's first transmission as a duplicate of
  partner A's. Caught because a test that passed alone failed in the suite —
  the suite runs several partners in one process.
- **A lane refusal crashed the response builder.** `throttled_271` assumed the
  AAA fields the partner-level bucket supplies; a lane refusal has none, the
  `KeyError` went unhandled, and the connection dropped rather than answering —
  so the client saw `RemoteDisconnected`, indistinguishable from a network
  fault. The handler now answers 500 with a body instead of dropping the socket.

## What is still missing, and why it cannot be closed here

- **No certification.** The 999 checks envelope integrity, required segments,
  required elements and code values — not implementation-guide situational
  rules, loop repetition limits, or CTX segments. A certification suite (Edifecs,
  Claredi) is licensed software and is not available offline.
- **No TA3, and TA1 duplicate detection is in-process.** The ISA control-number
  history lives in memory, so a restart forgets it and a replayed interchange
  from before the restart is accepted. Real duplicate detection needs a
  persistent, per-partner control-number store.
- **The throttle and lanes are single-node.** The buckets are a dict, so a
  second instance doubles every limit. Shared state (Redis, or a gateway that
  owns the counter) is the answer and needs infrastructure this does not have.
- **The dashboard has no time series and no persistence.** Counters reset with
  the process, so there is no trend and no way to answer "when did partner B's
  rejection rate change".
- **Agreement enforcement covers volume and lane only.** A real TPA also governs
  transaction types, hours of operation, test-versus-production separation,
  connectivity method and certificate rotation.
- **The HTTP layer has no authentication.** The trading partner comes from a
  header anyone can set, so the throttle is an honesty system rather than a
  control. Real EDI connectivity is mutually authenticated — AS2 certificates,
  SFTP keys, mTLS — and identity has to come from the transport.
- **No 837, 835 or 834.** The claims store is populated by a direct
  `adjudicate()` call rather than by the transaction that would really create
  it, and those transaction sets are large enough to be their own project.
- **No real adjudication engine.** One contracted rate (62% of billed), no fee
  schedules, no bundling, no COB, and no prior-auth enforcement despite the
  `requires_auth` column existing.
- **Five members and two plans.** Enough to exercise every branch, not enough
  to say anything about scale.

## Files

| path | what |
|---|---|
| `src/x12.py` | segments, envelopes, control numbers, code tables (AAA, EB01, STC) |
| `src/payer.py` | members, coverage spans, benefits, accumulators, adjudication |
| `src/transactions.py` | 270/271, 276/277, and the ambiguity-preserving facade |
| `run_demo.py` | five answer states, accumulator linkage, round-trip, batch |
| `src/throttle.py` | TPA-driven token bucket; refusal expressed as AAA 42/R |
| `serve.py` | HTTP facade (X12 and JSON), throttle, transaction dashboard |
| `demo_http.py` | exercises both facades and the throttle in both |
| `src/ta1.py` | interchange acknowledgement, and one-directional priority lanes |
| `tests/test_ta1.py` | 19 tests: the TA104 derivation, and batch not starving interactive |
| `tests/test_http.py` | 23 tests: the bucket, the X12 refusal, the dashboard |
| `tests/test_edi.py` | 36 tests |
