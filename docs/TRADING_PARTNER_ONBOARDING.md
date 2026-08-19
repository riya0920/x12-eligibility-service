# Trading partner onboarding

The document an integration team actually maintains. A provider system, a
clearinghouse, or a practice-management vendor connecting to this payer works
through it top to bottom, and most of the questions that arrive by email during
an integration are answered somewhere in here.

It is deliberately written as a **companion to a trading partner agreement**,
not a substitute for one. The TPA is the commercial and legal instrument; this
is the technical appendix that makes it operable.

---

## 1. Before you write any code

| you need | from | why |
|---|---|---|
| Trading Partner Agreement (TPA), signed | contracting | governs volume, hours, liability, and what happens when one side breaks the other |
| Interchange qualifier + ID (ISA05/ISA06) | this team | how we recognise you; a wrong ID rejects the whole interchange |
| Our qualifier + ID (ISA07/ISA08) | this table below | |
| Test connectivity credentials | this team | test and production are separate endpoints with separate IDs |
| A named technical contact **on each side** | both | the single most common cause of a stalled integration is not knowing who to call |

**Our identifiers**

| element | value |
|---|---|
| ISA07 (receiver qualifier) | `ZZ` |
| ISA08 (receiver ID) | `PAYER` (15 chars, space-padded) |
| GS03 (application receiver) | `PAYER` |
| Implementation convention (GS08) | `005010X279A1` for 270/271 |

**Scope note, up front:** this implementation is structurally honest but **not
certification-complete**. It is not tested against an X12 certification suite,
does not implement every situational rule in the implementation guide, and
should not be treated as a certified HIPAA transaction implementation. A real
onboarding includes certification testing and this document would name the
certifying body and the test results.

---

## 2. What we support

| transaction | direction | we support |
|---|---|---|
| 270 eligibility inquiry | you → us | real-time and batch |
| 271 eligibility response | us → you | real-time and batch |
| 276 claim status inquiry | you → us | real-time and batch |
| 277 claim status response | us → you | real-time and batch |
| 999 implementation acknowledgement | us → you | always, for every interchange |
| TA1 interchange acknowledgement | — | **not implemented** |
| 837 / 835 / 834 | — | **not implemented** |

**You will always get a 999.** Do not build a workflow that treats "no 999" as
success — build one that treats it as an incident, because it means either we
never received the file or we could not answer.

---

## 3. The two acknowledgements, and why they are different

This is the distinction most new integrations get wrong.

| question | answered by | example |
|---|---|---|
| **Was my file syntactically usable?** | **999** | missing required segment → `IK5*R` |
| **What is the business answer?** | **271** | member not found → `AAA*Y**75*C` |

A member who does not exist is a **business** outcome and comes back as a 271
with an AAA segment. A missing required element is a **syntax** outcome and
there is no 271 at all — the transaction never reached adjudication.

If your system cannot distinguish these, it cannot tell *"we processed your
inquiry and the answer is no"* from *"we could not read your file"*. Those need
different responses from different teams, and conflating them is how a front
desk turns a covered patient away because a pipe character broke a segment.

**999 acknowledgement codes**

| IK5/AK9 | meaning | what you should do |
|---|---|---|
| `A` | Accepted | nothing; expect the 271 |
| `E` | Accepted, errors noted | fix the reported elements; the transaction *was* processed |
| `R` | Rejected | fix and resubmit; **no 271 is coming** |

`IK3` and `IK4` carry the **segment position and element number**, so a
rejection is a ticket someone can close rather than a mystery. "Your file was
rejected" is useless; "segment 14, element 1, invalid code value `ZZ`" is
actionable.

---

## 4. Reading a 271 correctly

**Do not model eligibility as a boolean.** There are five distinct answers and
the difference between them is operational:

| `EB01` | meaning | what the front desk does |
|---|---|---|
| `1` | Active coverage, benefit known | collect the copay shown |
| `U` | **Contact following entity** | call the named administrator — **this is not a denial** |
| `I` | Non-covered service | member pays, or appeal |
| `6` | Inactive on the date of service | check for other coverage |
| *(AAA present, no EB)* | inquiry rejected | correct and resubmit per AAA04 |

The `U` row is the one that causes patient harm when it is collapsed. A benefit
administered by a carve-out vendor is **not** "not covered", and a front desk
told the latter turns away a patient who has the benefit.

**Accumulators are point-in-time estimates.** Deductible and out-of-pocket
figures in a 271 reflect claims adjudicated *at the moment of the inquiry*.
Claims in flight are not included. Do not quote them to a member as a final
amount — this is the single most common source of provider-billing disputes
arising from eligibility data.

**AAA rejections you should expect and handle**

| AAA03 | meaning | AAA04 | typical fix |
|---|---|---|---|
| `75` | Subscriber/insured not found | `C` | check the member ID |
| `71` | Birth date does not match | `C` | check the DOB — **the member exists** |
| `73` | Name does not match | `C` | check spelling |
| `72` | Invalid/missing subscriber ID | `C` | required field empty |
| `42` | Unable to respond at current time | `Y` | retry later; do not resubmit immediately |

`75` and `71` must be handled differently. `75` means we have no such member —
check the ID. `71` means **we have that member and your date of birth is
wrong**. A system that shows "not eligible" for both sends the patient away in
the second case, when the fix is re-keying one field.

---

## 5. Real-time and batch

**Real-time** is for a patient standing at a desk. One inquiry, one response,
synchronous.

**Batch** is for tomorrow's schedule. A file of many 270s in, a file of many
271s out, with **control totals reported on both sides**. Reconcile the counts:
if you sent 4,000 inquiries and received 3,997 responses, three members are
about to arrive without verified coverage and nobody will know until they do.

**Why batch still exists in 2026**, since this question arrives in almost every
onboarding call:

- **Volume economics.** A nightly file of 400,000 inquiries costs a fraction of
  400,000 real-time calls, on both sides.
- **Legacy trading partners.** A meaningful share of practice-management systems
  speak files and only files, and will for years.
- **It is sufficient.** Tomorrow's scheduled appointments do not need
  sub-second answers, and an overnight refresh is genuinely the right tool.

It is a different workload, not technical debt.

---

## 6. Volume, throttling, and what the TPA governs

**This is a commercial question before it is a technical one.**

If a provider system sends 10,000 inquiries an hour for a population we mostly
do not cover, the technical options are throttle, reject, or serve — and the
right answer is not chosen by the engineer on call. It is in the TPA, which
should specify:

- expected and peak transaction volumes
- the agreed real-time response-time target
- batch windows and file-size limits
- what happens when volumes exceed the agreement

**Our position:** serve within the agreed rate; queue and respond with `AAA*42`
(unable to respond at current time, follow-up `Y` — wait and resubmit) beyond
it; escalate persistent overage commercially rather than degrading silently.
Silent degradation is the worst option because the partner cannot tell it is
happening and will diagnose it as their own problem.

**Not implemented in this build:** no rate limiting, no quota tracking, no
per-partner throttle. The policy is written down; the enforcement is not, and
that gap is stated rather than implied.

---

## 7. Test-to-production promotion

1. **Connectivity** — exchange one well-formed 270 in the test environment, get
   a 999 with `IK5*A`.
2. **Happy path** — a member we both know is covered; confirm you parse the EB
   segments and the accumulators.
3. **The rejection paths** — deliberately send a bad member ID, a wrong DOB, and
   a terminated member. Confirm you distinguish `75`, `71` and `EB01*6`, and
   that your UI shows something different for each. **This step is where most
   integrations are actually found to be broken**, because the happy path works
   early and nobody exercises the other 70% until go-live.
4. **The `U` case** — a carve-out benefit. Confirm your UI does not render it as
   a denial.
5. **Batch** — a file with a deliberate control-total mismatch; confirm you
   detect it rather than processing a short file as complete.
6. **Volume** — a run at your stated peak rate.
7. **Production credentials** issued only after 3, 4 and 5 pass.

ISA15 (`P` production / `T` test) must match the environment. Sending `P` into
test, or `T` into production, is a common and confusing failure — some partners
route on it.

---

## 8. When something breaks

| symptom | first thing to check |
|---|---|
| no 999 at all | connectivity, and whether the file arrived |
| `IK5*R` on everything | ISA/GS identifiers, then the implementation convention in GS08 |
| 999 accepted, no 271 | our side; open a ticket with the ISA13 control number |
| 271 says "not found" for known members | member ID format, then whether you are hitting test with production IDs |
| batch counts do not reconcile | your control totals against ours before assuming data loss |

**Always quote the ISA13 interchange control number.** It is the only identifier
that lets both sides find the same file, and a ticket without one starts with an
hour of searching.
