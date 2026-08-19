# SE-2 — Eligibility & claim status (X12 EDI facade) (first 20%)

270/271 and 276/277 with real envelopes, real control numbers, and real AAA
rejection semantics — behind a JSON facade that **refuses to flatten an
ambiguous answer into a boolean**.

```bash
python run_demo.py         # real-time + batch, accumulators, round-trip
python -m pytest tests -q  # 27 tests
```

Offline, under a second, standard library only.

## Scope boundary, stated first

This is **structurally honest, not certification-complete**. Real envelopes,
real loops, real code values — and *not* a HIPAA-compliant transaction
implementation. Missing: the X12N implementation guide's situational rules, full
999/TA1 acknowledgement, and any certification testing.

Saying so is the mature move. Claiming transaction fidelity that has not been
tested against a certification suite is the overclaim that ends a screen,
because the first person who has actually done this work asks which guide
version and what the 999 rejection rate was.

---

## The four things worth reading

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

---

## What is missing (the other 80%)

- **No certification.** No 999 or TA1 generation beyond structural checking, no
  implementation-guide situational rules, no testing against a certification
  suite. The scope note above is the honest boundary.
- **No trading-partner onboarding doc.** The spec asks for one — the artefact
  integration teams actually maintain, covering ISA qualifiers, IDs, connectivity,
  test-to-production promotion, and volume expectations. Not written.
- **No transaction dashboard.** Volume, rejection rate by AAA code, and response
  times are computed but only printed; there is no UI and no time series.
- **No throttling or trading-partner agreement enforcement.** The spec's
  10K-inquiries-per-hour question is unaddressed in code. The answer is that a
  TPA governs volume, and the response is to serve within the agreed rate and
  escalate commercially rather than silently degrade — but none of that is
  implemented.
- **No HTTP layer.** The "REST facade" is a Python function returning a dict.
  No auth, no rate limiting, no HTTP semantics.
- **No 837 claim submission**, no 835 remittance, no 834 enrolment — so the
  claims store is populated by a direct `adjudicate()` call rather than by the
  transaction that would really create it.
- **No real adjudication engine**: one contracted rate (62% of billed), no fee
  schedules, no bundling, no COB, no prior-auth enforcement despite the
  `requires_auth` column existing.
- **Five members and two plans.** Enough to exercise every branch, not enough to
  say anything about scale.

## Files

| path | what |
|---|---|
| `src/x12.py` | segments, envelopes, control numbers, code tables (AAA, EB01, STC) |
| `src/payer.py` | members, coverage spans, benefits, accumulators, adjudication |
| `src/transactions.py` | 270/271, 276/277, and the ambiguity-preserving facade |
| `run_demo.py` | five answer states, accumulator linkage, round-trip, batch |
| `tests/test_edi.py` | 27 tests |
