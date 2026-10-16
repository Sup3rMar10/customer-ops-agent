# Onboarding runbook: rolling the customer ops agent out at a customer

How I would take this from "signed" to "the AR team relies on it", and what I would refuse to skip. Written for the customer's sponsor, their security lead and my own team. Durations are typical ranges, not promises: the real ones depend on how fast the customer can grant access.

The principle behind every phase: **each step widens what the assistant can do only after the previous step has produced evidence that it is safe and useful.** Reading before writing, shadow before live, a few named users before everyone.

## 0. Who is who

| Role | On the customer side | On our side |
|---|---|---|
| Sponsor | head of AR / finance operations: owns the outcome and the success numbers | account lead |
| Day-to-day owner | AR team lead: labels examples, reviews proposals | forward deployed engineer (me) |
| Systems | ERP / billing admin: grants access, explains the data | engineer |
| Security and privacy | InfoSec and privacy contact: signs off access and data handling | security reviewer |
| Operators | 2 to 3 named AR analysts who may approve write proposals | trained by me |

## 1. Discovery (about 1 to 2 weeks)

Goal: understand the work and the system well enough that the first demo uses their words and their data shape, not mine. I replace my guessed data model with theirs here.

**The work**
- Which five questions does the AR team answer most often? Can I sit in on a day of tickets or calls?
- Who asks (internal analysts, collectors, customers' own AP staff)? Through what channel?
- What does a good answer look like, and what is the cost of a wrong one? (A wrong balance quoted to a customer is worse than a slow answer.)
- What do analysts do today to find a balance, a dispute reason, a payment history? How long does it take?
- Which actions follow from the answers (reminders, dispute notes, credit holds), and which of those are safe for a machine to *propose*?

**The systems**
- Which system is the source of truth for invoices, payments, disputes and contacts? Is there more than one?
- Does it have an API, a reporting database or only exports? Rate limits? Sandbox? Change windows?
- How are statuses defined (is "overdue" computed from due date, or a stored flag)? How are partial payments, credit memos, write-offs and voids represented?
- Currencies, tax, multiple entities, fiscal calendars?
- Where do free-text notes live, and who can type into them (customers, portals, email imports)? Anything outside the company writing into those fields is an injection route.

**Data and security**
- Which fields are personal data (names, emails, phones)? Are there payment-card, bank or health data anywhere near these tables? (If yes, they are excluded from the assistant's reach by design, not just masked.)
- Retention and residency rules? Where may logs live, and for how long?
- Which model providers are approved, and is data allowed to leave their network?
- How do they approve changes today (tickets, chat approvals)? That is where my approval step should live.

**Exit:** a one-page scope (questions in, actions out, data excluded), a list of the five to ten real fields and endpoints, and a named owner on each side.

## 2. Data access and security set-up

- **Least privilege.** A dedicated read-only service account per environment. Write access is a separate credential, off until Phase 3, scoped to creating notes and drafts only.
- **Sandbox first.** Everything is built and tested against a sandbox or a masked copy. Production access comes after the security lead signs off on the audit log and the masking rules.
- **Per-user permissions.** Pass the asking user's identity through so a user can only see accounts they may already see. A shared all-seeing key is a stop-ship finding.
- **Mask by default.** Contact details are masked unless the privacy contact approves a specific field for a specific role.
- **Secrets.** The approval secret and any API keys live in the customer's secret store, rotated on a schedule and on staff change.
- **Network.** The HTTP transport has no authentication of its own, so it sits behind their authenticating proxy or is reachable only on a private network.
- **Audit.** The JSONL audit log (timestamp, session, tool, masked arguments, outcome, error code, latency, security flags) ships to their logging stack. Retention is set by their policy.
- **Review pack for their security team:** the guardrail layers, the ablation results, the list of known limits (see README), and a test they can re-run.

## 3. Rollout phases

Each phase has an entry gate and an exit gate. I do not start the next phase until the exit gate is met and the sponsor has seen the numbers.

### Phase 1: shadow mode (read-only, nobody sees answers) - about 2 weeks
- Run the assistant on real or replayed questions. Its answers go only to my team and the AR lead.
- Build the evaluation set from the customer's own tickets: the AR lead labels the right answer for at least a few dozen, spread across question types, with a held-out part nobody tunes against.
- **Exit:** correct-answer rate on the held-out set meets the agreed bar (set in discovery, for example "at least 95% of balance and status answers exactly right"), and every wrong answer is understood. Injection and masking tests re-run against the model they will actually use, and pass.

### Phase 2: read-only, a few named users - about 2 to 4 weeks
- 5 to 10 analysts use it for real work, with a way to mark an answer wrong in one click.
- Weekly review of every marked answer and a random sample of unmarked ones.
- **Exit:** weekly numbers stable for two weeks; no security finding open; analysts say it saves time (their words, with a number).

### Phase 3: propose-writes, with human approval - about 2 to 4 weeks
- Turn on the two write tools. They only propose. Operators approve inside the customer's own tool.
- Track how often proposals are approved as-is, edited, or rejected. A high rejection rate means the proposals are not yet good enough to be worth reading.
- **Exit:** approval-as-is rate and zero unapproved writes over the agreed period; operators confirm the review step costs less than doing the task by hand.

### Phase 4: broaden
- More users, more question types, one new action at a time, each repeating Phase 3 for that action.
- Anything that would let the assistant act without a human (auto-approve) is a separate decision with its own review, and is off the table until the numbers support it.

## 4. Success metrics

Agreed with the sponsor in discovery, reported weekly, including the bad weeks.

| Metric | How it is measured | Why it matters |
|---|---|---|
| Correct-answer rate | AR lead audits a random sample each week against the source system | the thing that decides trust |
| Time to answer | median and p95, assistant vs the baseline measured in discovery | the business case |
| Analyst time saved | short sampled time-and-motion check, not a survey alone | the other half of the business case |
| Proposal outcome | share approved as-is / edited / rejected, per action | whether writes are worth reviewing |
| Unsafe-action attempts | writes attempted without approval, tokens refused, injection flags raised | the safety layers working, and early warning |
| Escalation rate | share of questions the assistant could not answer and handed to a person | healthy if it is honest about what it does not know |
| Tool error rate and p95 latency | gateway audit log | the integration staying healthy |
| Adoption | weekly active users out of the invited group | whether it is actually used |

Baselines are captured before Phase 2 so improvement is measured against what they did, not against my guess.

## 5. Escalation and incidents

| Severity | Example | First response | Who |
|---|---|---|---|
| Sev 1 | an unapproved write is found; data shown to a user who should not see it; a credential leaks | **stop the assistant**: restart the server without `--allow-writes` or revoke the service credential; preserve logs; notify the customer's security lead | me, then my manager and theirs |
| Sev 2 | a confidently wrong balance or status reached a customer | pull the affected answer, find the cause from the audit log, tell the AR lead the same day | me |
| Sev 3 | a wrong answer caught by an analyst; a flaky upstream | log it, add it to the evaluation set, fix in the normal cycle | me |
| Info | a spike in injection flags on one customer's notes | check whether the notes are an attack or a false positive; tell the data owner | me |

The kill switch is deliberately simple and does not depend on the assistant: read-only mode is the default, and removing the write credential disables writes whatever the software does.

After any Sev 1 or 2: a written account within a few days (what happened, what the audit log shows, what changed), and the failing case added to the evaluation set so it cannot quietly return.

## 6. What tends to go wrong

- **The data model is not what the diagram said.** Statuses, credit memos and partial payments differ by system. Discovery and the first sandbox week exist to find this before anyone sees an answer.
- **Free text is the attack surface.** Notes typed by customers or imported from email can carry instructions. Treat them as data everywhere, and keep the approval step even when the screen looks clean.
- **Approval fatigue.** If operators rubber-stamp, the control is theatre. Watch the approve-as-is rate and the time spent per proposal.
- **A fast demo hides a slow integration.** Measure latency against the real system, not the mock.
- **Scope creep from a happy sponsor.** New actions go through the same phases.

## 7. Handover checklist

- [ ] Scope document signed off by sponsor and security lead
- [ ] Access: read-only and write credentials separated, scoped, rotatable, owned by the customer
- [ ] Masking and exclusion rules approved by the privacy contact
- [ ] Evaluation set built from their tickets, with a held-out part, and a command their team can run
- [ ] Audit log flowing to their logging stack with agreed retention
- [ ] Operators trained; approval lives in their tool
- [ ] Weekly metrics report agreed, with baselines recorded
- [ ] Incident contacts and the kill switch rehearsed once
- [ ] Known limits written down and shared, not just the wins
