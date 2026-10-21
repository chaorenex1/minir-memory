---
id: markers-category
version: 2
applies_to: [base:B4, knowledge_render]
slot: fields
max_tokens: 1000
sections: [contract, review_checks]
---

## CONTRACT

WHAT
One unit states one thing; the primary category says what kind of thing that is. Exactly one
category. A unit that answers two questions is two units. Category and org are independent: the
category never decides the org.

CATEGORIES
- KNOWLEDGE: what is, how it works, why. Facts, concepts, definitions, principles, laws, theories,
  mechanisms, measurements, verified explanations.
- VALUE: what is worth wanting or how good it is. Values, principles, standards, preferences,
  priorities, trade-offs, evaluations, judgements.
- INTENT: what a subject wants. Goals, needs, wishes, plans, purposes, expectations, target states.
- METHOD: how a task is done. Methods, techniques, skills, processes, strategies, commands,
  workflows, solutions.
- GOVERNANCE: what binds a subject. Rules, policies, constraints, permissions, prohibitions,
  obligations, norms, compliance requirements.

EPISTEMIC
- Required on KNOWLEDGE, forbidden elsewhere: fact observed here, inference derived from this
  window, hypothesis proposed and untested, prediction about a future state.
- "may happen" is a prediction, never a fact; an untested cause is not a fact.

DISAMBIGUATION (first match wins; both sides on one line)
1. what it is / whether it matters: "latency is 200 ms" K / "200 ms is too slow" V
2. what is real / what is wanted: "the system supports several agents" K / "we want several
   agents" I
3. why or what / how to operate: "an index cuts some query cost" K / "index this column" M
4. what is the case / what is allowed or required: "the user is an admin" K / "only admins may
   delete" G
5. worth pursuing / binding: "privacy matters" V / "never leak user data" G
6. evaluation stance / desired state: "reliability comes first" V / "reach 99.9% uptime" I
7. criterion or trade-off / implementation: "prefer low maintenance" V / "add automated tests" M
8. purpose or result / steps or means: "we want unattended deploys" I / "configure CI/CD" M
9. wants to do / must obey: "we plan to open the API" I / "the API must authenticate" G
10. how it is done / how it is mandated: "restore from a backup" M / "verify the backup first" G

SPECIAL
- Describing a norm is not the norm. A regulation quoted for what it requires is GOVERNANCE, and
  a report on what a rule says is usually still GOVERNANCE; only a study of its history, structure
  or cause makes it KNOWLEDGE. Decide by what the unit is for.
- Split a plan from its steps when both carry independent meaning: "test then deploy" is one
  INTENT plus one METHOD, not one unit with a secondary tag.
- Fact, inference, hypothesis and prediction stay apart inside KNOWLEDGE; never record "may
  happen" as "has happened".

TOPIC
- Noun phrase, 2 to 40 characters, naming the object and the property that matters. It is the
  dedupe key and the knowledge filename, so it stays stable across revisions.
- No sentence, question, bare verb, "misc", "other", "TBD".

IDENTITY
- category + org + topic. The same triple revises that unit; the same topic under another category
  is a different unit.

## REVIEW CHECKS
- Two primary categories, or the category picked from a noun rather than from the claim.
- KNOWLEDGE without epistemic, or epistemic on another category.
- An evaluation written as a fact, or a goal written as a fact ("supports X" for a planned feature).
- A plan and its steps in one unit.
- A "must", "only" or "never" sentence filed as KNOWLEDGE, or a description filed as GOVERNANCE.
- topic is a sentence, a question, a placeholder or over 40 characters.
- A prediction or hypothesis recorded as an observed fact.
