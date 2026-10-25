---
id: markers-org
version: 5
applies_to: [base:B4, knowledge_render]
slot: fields
max_tokens: 1200
sections: [contract, review_checks]
---

## CONTRACT

WHAT
The org is the retrieval address: the name a later session types to find the unit again, and the
directory the page lands in. Choose it in two steps, time shape first and object second. One primary
org. Category and org stay independent: any category can sit under any org.

STEP 1, TIME SHAPE
Ask whether the unit reports something that happened: an action, with a date, an object and a result.
- No: a standing unit, go to step 2.
- Yes, and it is two or more dated states of one object in order: trajectories.
- Yes, and it is one occurrence:
  events is the work log: who did what, to which object, with what result. Every action a reader
  could ask about gets one events unit, even when nothing was learned from it. "What did I do last
  week" and "what did we do on this project" are normal questions, and the record does not need a
  date lookup to earn its place.
  experiences is the same occurrence only when it changed how the subject works: a conclusion, a
  correction to a plan, a pitfall worth reusing. Write the lesson, not the action.
  When an occurrence produced both a record and a reusable conclusion, write two units: events for
  what was done, experiences or method for what it taught.

STEP 2, OBJECT (standing units, first match wins)
- tools: a capability that can be invoked. CLI, API, plugin, model, database: what it does, its
  inputs and limits, where it worked and where it failed. "What can I use, how do I call it."
- entities: a subject with identity: person, organisation, service, system, device, place. Its
  properties, relations, configuration and scope. "Who or what is it."
- projects: sustained work with a goal. Only when the unit can name a goal, a deliverable and a
  current state; if it cannot name all three it is not a project unit, and it files as whatever it
  actually states, usually the experience that produced it or the entity it configures.

TIE BREAK
Take the name a later session would type first. A tool lesson is tools, a project decision is
projects, a deployment with a result is events, a constraint on how the user works is entities (the
person), not projects.

EXAMPLES
- the user's profile character budget: entities, GOVERNANCE (a limit on the person, not a project).
- an upgrade's goal and its binding constraints: projects, INTENT and GOVERNANCE, two units.
- the result limit on a search tool: tools, KNOWLEDGE.
- a failed deployment and its rollback: events, KNOWLEDGE of the occurrence; the rollback steps are
  a separate method unit.
- a certificate issued on a date with an expiry: events, because the issuance is a record; the
  renewal procedure is a separate method unit.

OBJECTS
- Other objects the unit touches, as "type:name" in the singular ("tools:Docker"), up to five. The
  primary org is not repeated. The type is one of the orgs; the name is not a sentence.

RELATIONS
- supersedes, caused_by, part_of, contradicts, updates, instance_of, derived_from, same_as. A
  relation to a unit in this window names its id; an `updates` relation names the existing page
  path or topic it merges into; `derived_from` names the event or unit a lesson came from;
  `same_as` marks an alias of the same unit. A target outside both is dropped, never invented.
  "caused_by" needs a stated cause or a verified diagnosis.

BOUNDARIES
- One primary org: two means two units or one wrong choice. Never duplicate a unit under two orgs for
  findability; objects and relations carry the link. The org says where it is filed, not what it
  says.

## REVIEW CHECKS
- A dated occurrence filed under projects or entities, or a standing property filed under experiences.
- A project unit that cannot name a goal, a deliverable and a state.
- A window whose actions produced no events unit, or an events unit without an object or a result.
- An experiences unit that reports an action without the conclusion it produced.
- One action split into several events, or repeated identical actions filed as separate pages instead
  of appended to the same one.
- Two dated states of one object filed as one unit.
- The org chosen from the category ("it is INTENT, so projects") instead of from the address.
- objects without "type:", plural, or repeating the primary org.
- relations pointing outside this window and outside a named page, or a cause the content never
  states.
- The same content under two orgs.
