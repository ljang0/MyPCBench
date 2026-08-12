# Live-VM Rubric Feasibility Audit — 2026-08-12

Empirical follow-up to `TASK_RUBRIC_HEALTH_HANDOFF.md`. That handoff delivered
static task/rubric/date-contract validation only; this audit checked **every
rubric item of every task against a live VM** booted from the current daily
image, and fixed what failed — rubric-only, per the standing decision to
prefer rubric changes over image or instruction changes.

## Provenance

- Image: `ljang/mypcbench-qemu:latest`, baked 2026-08-12T21:00:19Z (age 0h at
  boot), fetched via skopeo; qcow2 decompressed.
- VM date at audit: 2026-08-12 (Wednesday), EDT. Persona
  `michael.scott@dundermifflin.com`, all 17 app DBs prewarmed.
- Method: read-only probing through the VM control API — in-guest sqlite
  (`/data/vms/michael_scott/*.sqlite`), signed-cookie HTTP against each app's
  API, guest filesystem inspection, and app-source route inspection for
  feature-existence questions. **No task was executed by an eval agent and no
  judge was invoked**; this certifies that every rubric's target exists and is
  achievable/judgeable, not that agents complete the tasks.

## Sweep results (all 184 tasks, 1,135 rubric items pre-fix)

| Verdict | Items | Meaning |
|---|---:|---|
| FEASIBLE | 646 | target verified present and aligned on the live image |
| ACTION_ONLY | 433 | grades an agent action; its precondition/target verified present |
| MISALIGNED | 20 | required something the instruction never asked |
| AMBIGUOUS | 21 | multiple live matches / undefined boundary where the rubric assumes one |
| INFEASIBLE | 13 | referenced entity/feature absent or window unsatisfiable on the live image |
| ROLLOVER_RISK | 2 | true today, flips on a future daily rebake |

56 defective items across 40 tasks. All 56 were rewritten (55 in place, 1
merged into a sibling; a handful of sibling items were touched for
consistency). Item count is now **1,134**. Weights re-verified to sum to
exactly 1.0 (Decimal) on every touched task; `scripts/validate_tasks.py`
(new, committed) is green across all 184 tasks.

## Fix patterns applied

- **Misalignment**: dropped or softened unasked specifics (app choice, UI
  label wording, extra artifacts, process steps) to accept any reasonable
  route to the asked-for outcome.
- **Ambiguity**: accept any defensible reading (e.g. "next Friday/Wednesday"
  boundary on a matching weekday; multiple matching variants/bookings) or pin
  to a rule discoverable in-app.
- **Infeasibility**: retargeted to live-verified equivalents ("Cara Mia
  Trattoria", available product categories, live thread subjects) with
  explicit verified-absence/report branches where the instruction's named
  target may not exist.
- **Rollover risk**: replaced hardcoded amounts/counts with derivations from
  live app data so criteria hold on every bake.

## Cross-cutting live findings (grader/infra guidance, no task change)

- `/data/<app>.sqlite` are baked templates; per-persona copies live under
  `/data/vms/michael_scott/`. Several apps (Gringotts, SpeedTax, Dinoco,
  Cheskepdia at minimum) serve template-flavored data that diverges from the
  per-persona copy. **The app HTTP API is the only authoritative state**;
  rubric judging must never rely on backend sqlite reads.
- Confirmed live: `batbucks.sqlite` and `oddsmarket.sqlite` are frozen at
  their Jul 19 bake (not in the daily rebase allowlist). The affected tasks'
  contracts treat their data as frozen history, so this is safe — but their
  relative wording ("year-end") goes semantically stale after 2027-01-01.
- BuzzChat globally rebases, but the Party Planning Committee group's
  newest message stays ~8 weeks old while `last_message_time` claims recent
  activity (affects "recent message" phrasing; rubrics now accommodate).
- Seed wart: Jamaica receipt email subjects carry the pre-rebase trip dates
  while the live booking rebakes 3 days later (see flags below).

## Flagged for owner decision

**Resolution (same day, owner-approved):** items 1–5 and 7 below were fixed
with surgical instruction edits verified against the live image (LockedIn
draft → save-text-locally-and-don't-post; f046 subject described instead of
quoted; HooliShop items retargeted to live products — Hawaiian shirt 3-pack,
Travel Adapter EU/UK, pens/copy paper; f016 fallbacks now Alfredo's Pizza
Cafe, Cara Mia Trattoria, State Street Grill; f008 instruction now
acknowledges the calendar/booking date mismatch and asks for removal of
whatever Jamaica entries exist). Item 6 (`contradiction-f011`) is accepted
as-is for now: the rubric grades the live comparison either way; the
deterministic-contradiction guarantee awaits a year-preserving rebake rule
whenever the rebake code is next touched. The f008 seed offset itself
remains an image-side wart (cosmetic after the instruction fix).

Original flags (instructions/seeds referenced things the live image
contradicted):

1. `long_horizon-f040`, `long_horizon-f070`, `long_horizon-f075` — instruction
   requires leaving a LockedIn post as an unpublished **draft**; LockedIn has
   no draft feature (no schema column, no API route).
2. `long_horizon-f046` — instruction quotes an exact email subject that exists
   nowhere; the live thread is "Re: Expense Report Q2 — Flagged Items
   [JAN-EXP-Q2]".
3. `long_horizon-f054`, `long_horizon-f074` — instruction asks for HooliShop
   notebooks (and coffee in f054); neither exists in the 92-product catalog.
4. `long_horizon-f070` — additionally names two HooliShop products that don't
   exist ("Men's Classic Fit Oxford Shirt"; adapter exists only as "Travel
   Adapter EU/UK").
5. `hard_app-f016` — instruction names fallback restaurants "Cugino's"
   (absent) and "Cara Mia Bistro" (live name "Cara Mia Trattoria").
6. `contradiction-f011` — the authored SpeedTax-vs-Gringotts donation claim
   drifts across the CY2025 boundary as dates rebake; needs year-preserving
   rebase or bake-time re-authoring of the claim amount.
7. `situated_action-f008` — seeded Jamaica calendar block sits 3 days before
   the live trip window (seed offset).

## Deltas vs base `caf9c754` (whole branch, after this audit)

- 103 tasks changed (76 instructions — unchanged by this audit); 102 task
  rubric sets changed (78 from the static remediation + 24 newly touched).
- Rubric items: 1,192 → 1,134.

## Not done / next

- No eval-agent execution, no judge scoring (out of scope by design).
- Recommended: re-run the probe suite after the next daily rebuild — any
  verdict flip is a real rebake sensitivity. The audit harness (probe helper,
  group manifests, verdict files) is reproducible from this doc's method.
