# Eligibility & Claim Status (X12 EDI facade)

## What it is

When a patient checks in, the clinic asks the insurer: **"Is this person covered,
and what will they owe?"** That question travels as an X12 EDI message (a strict
text format insurers have used for decades). The 270 asks about coverage, the 271
answers. The 276 and 277 do the same for claim status.

We built both sides: the messages with real envelopes and control numbers, a
small insurer (payer) database behind them, and a JSON web service in front so an
app never has to read raw X12.

The main idea: **"is this person eligible?" has five answers, not two.** A
yes/no answer throws away the part the front desk needs, and can make them turn
away a patient who is actually covered.

## What we did

1. **Built the X12 layer**: segments, envelopes, nested control numbers, and the
   code tables (AAA reject reasons, EB01 benefit codes, STC claim status codes).
2. **Built a small payer**: members, coverage dates, benefits, and running
   deductible and out-of-pocket totals that claims update.
3. **Wrote 270/271 and 276/277** plus a JSON facade that keeps all five answer
   states and never returns a plain `eligible: true/false`.
4. **Added rejections with a next step**: 17 AAA reject codes, each telling the
   sender what to fix.
5. **Added the 999** (a "was your file readable?" reply, separate from the
   business answer) with the exact segment and element of each error.
6. **Added batch mode**: watch a folder, turn 270 files into 271 files, and check
   that counts in and out match.
7. **Added an HTTP service, a rate limit, and a dashboard**, where a refusal is
   sent back in X12 form the partner's system can read.
8. **Added the TA1** (the reply for a broken outer envelope) and **priority
   lanes** so a bedside check is not blocked by an overnight batch.
9. **Wrote a trading-partner onboarding guide**, the document an integration team
   actually keeps.

Work was done in several passes. The later passes closed gaps named by the earlier
ones, and testing found real bugs along the way.

## Results

**Answer states**

| situation | JSON `benefit_determination` | what the front desk does |
|---|---|---|
| covered, benefit known | `active_coverage_benefits_known` | collect the copay |
| covered, handled by another company | `unknown_contact_other_entity` | call that company |
| covered, this service is not | `service_not_covered` | member pays, or appeal |
| coverage ended before the visit | `coverage_inactive_on_date_of_service` | check other coverage |
| inquiry rejected | `inquiry_rejected` | fix and resubmit |

There is no `eligible` key anywhere, and a test checks that.

**Rejections and replies**

- 17 AAA reject codes, each with a follow-up action.
- "No such member" (AAA 75) and "wrong birth date" (AAA 71) are kept apart. The
  second one means re-type one field, not send the patient away.
- A rejected 271 has no benefit lines at all.
- 999 results: well-formed file `A` (accepted), bad service code `E` (accepted
  with errors), missing required segment `R`, tampered control number `R`.
- TA1 results: good envelope `A 000`, truncated `R 023`, garbage `R 024`,
  replayed `R 025`.

**Claims and totals**

- One claim (billed $4,200, allowed $2,604) moved the remaining deductible from
  $1,500.00 to $0.00 and out-of-pocket from $6,000.00 to $4,279.20. The next 271
  shows the new numbers.
- Twelve claims in a row never charge the member past the out-of-pocket cap.

**Round-trip and batch**

- Round-trip (build, write out, read back): **6 of 6** transactions identical.
- Batch: 5 in, 5 out, balanced. 1,636 transactions/sec on one core against an
  in-memory database (a correctness demo, not a benchmark).

**Tests:** 78 passing.

**Bugs found by testing (and fixed)**

- Text that was not X12 at all came back as "subscriber not found", an answer
  about a member for something that was never a message. Now the envelope is
  checked first.
- A regex bug made **every valid** interchange fail as "file truncated".
- Duplicate checking was global, so partner B's first file was rejected as a
  copy of partner A's. It is now per sender.
- A lane refusal crashed the reply builder and dropped the connection, which
  looked like a network fault. It now answers with an error body.

## Key decisions and why

**Keep five answer states, never a boolean.**
"Not covered" and "another company handles this" are both "not yes". Mixing them
up tells a front desk to deny care the member really has.

**Keep syntax errors and business answers in separate channels.**
A 999 says "we could not read your file". A 271 says "we read it and the answer is
no". Different teams fix these, so a partner must be able to tell them apart.

**Refuse over the rate limit in X12, not only HTTP.**
A partner's system may only understand 271s. A plain HTTP 429 looks like a
timeout, and timeouts get retried. So we also send a 271 with AAA 42 ("try again
later").

**Treat rate limits as contract terms.**
The limits come from the trading-partner agreements in code, so the code and the
onboarding guide cannot drift apart. Refill is continuous, so a partner cannot
send double their allowance across an hour boundary.

**Reserve 30% for interactive traffic, one way only.**
Interactive checks may borrow idle batch capacity. Batch may never borrow the
interactive reserve, or the nightly batch would spend it every night.

**Return the raw 271 alongside the JSON.**
When a provider says "your system told us the patient was covered", the answer
has to be the message that was actually sent.

**Unknown member is HTTP 200 with a rejection, not a 404.**
The request worked; the answer is "not found, check the ID". A 404 mixes that up
with "we could not process your request".

**Say the totals are point-in-time.**
Here claims and eligibility share one database, so reads are up to date. Real
payers often are not, so every answer warns that remaining deductible is an
estimate.

## Limits

- **Not certified or HIPAA-compliant.** No implementation-guide situational
  rules, loop limits or CTX segments. Certification tools are licensed software.
- No TA3. TA1 duplicate checking lives in memory, so a restart forgets it.
- Rate limit and lanes are single-server. A second copy doubles every limit.
- Dashboard has no history. Counters reset on restart.
- Agreements only enforce volume and lane, not hours, transaction types or
  test/production separation.
- **No authentication.** The partner name comes from a header anyone can set.
- No 837, 835 or 834. Claims are added by a direct function call.
- Simple adjudication: one rate (62% of billed), no fee schedules, bundling,
  coordination of benefits or prior-auth checks.
- Five members and two plans. Enough to hit every branch, not to say anything
  about scale.

## How to run

```bash
pip install -r requirements.txt   # only pytest; the code is standard library
python run_demo.py                # answer states, totals, 999, round-trip, batch
python serve.py --demo            # HTTP facade, both formats, the rate limit
python serve.py                   # serve on :8088, dashboard at /dashboard.html
python -m pytest tests -q         # 78 tests, offline
```

Full write-up for partners: [TRADING_PARTNER_ONBOARDING](docs/TRADING_PARTNER_ONBOARDING.md)

## Layout

```
src/x12.py           segments, envelopes, control numbers, code tables
src/payer.py         members, coverage, benefits, totals, adjudication
src/transactions.py  270/271, 276/277, and the JSON facade
src/ack999.py        999 syntax acknowledgement
src/ta1.py           TA1 envelope acknowledgement, priority lanes
src/throttle.py      rate limit from partner agreements, AAA 42 refusal
serve.py             HTTP service (X12 and JSON) and dashboard
run_demo.py          end-to-end demo
demo_http.py         exercises both facades and the rate limit
```
