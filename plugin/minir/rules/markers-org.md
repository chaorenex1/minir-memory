---
id: markers-org
version: 2
applies_to: [base:B4, knowledge_render]
slot: fields
max_tokens: 1000
sections: [contract, review_checks]
---

## CONTRACT

WHAT
What the unit is organised around: the object or activity you would look up to find it again. That
is the org, and it is the directory the knowledge page lands in. One primary org. Category and org
are independent: any category can sit under any org.

ORGS (each answers a different question)
- entities: who or what something is. People, organisations, agents, services, systems, devices,
  models, places: identity, properties, relations. Answers "what is it". Spans a long life.
- trajectories: how an object or system moves over time. Skill growth, project progress, a task's
  execution, a decision from proposal to revision, version history. Answers "how did it change".
  Strongly ordered in time.
- experiences: what happened once and what it proved. A task done, a deployment, a failure, a
  diagnosis, a review, the lesson taken. Answers "what happened, with which result". Past, single.
- tools: which capability can be invoked and how. Software, API, CLI, plugin, model, database,
  workflow: inputs, outputs, limits, dependencies, the context where it worked or failed. Answers
  "what can I use, how do I call it".
- projects: which sustained work serves a goal. Objective, scope, phases, milestones, deliverables,
  members, technical decisions, constraints, risks, acceptance, status. Answers "what is being
  built, in which state". Has a lifecycle.

SELECTION (first match wins)
- Identity or a property of an object: entities.
- State changing between points in time: trajectories.
- One event, its result and its lesson: experiences.
- A capability and how to invoke it: tools.
- Sustained work with a goal and a deliverable: projects.
- When two fit, take the object a later session would search for. A tool lesson files under tools;
  a project decision files under projects.

EXAMPLES
- who the user is: entities, KNOWLEDGE.
- a deployment project's goal and constraints: projects, INTENT plus GOVERNANCE.
- how to call Docker: tools, KNOWLEDGE of it plus METHOD of use.
- a version conflict broke the deploy: experiences, KNOWLEDGE for the event and for the cause once
  verified.
- a failing deploy that recovered: trajectories, KNOWLEDGE of the state change.
- pinning the version after the failure: experiences or projects, INTENT plus METHOD.

OBJECTS
- Other objects the unit touches, as "type:name" in the singular ("tools:Docker"), up to five. The
  primary org is not repeated. The type is one of the five orgs; the name is not a sentence.

RELATIONS
- supersedes, caused_by, part_of, contradicts, between units in this window; a target outside it is
  dropped, never invented. "caused_by" needs a stated cause or a verified diagnosis.

BOUNDARIES
- One primary org: two means two units or one wrong choice. Never duplicate a unit under two orgs
  for findability; objects and relations carry the link. The org says where it is filed, not what
  it says.

## REVIEW CHECKS
- An experience recorded as a trajectory, or a trajectory collapsed into one experience.
- A tool lesson recorded as a project, or a project decision filed under the tool.
- An entity that is really a project; a project that is really a pile of events.
- objects without "type:", plural, or repeating the primary org.
- relations pointing outside this window, or a cause the content never states.
- The same content under two orgs.
- The org chosen from the category ("it is INTENT, so projects") instead of from the object.
