---
id: markers-org
version: 1
applies_to: [base:B4]
slot: fields
max_tokens: 400
sections: [contract, review_checks]
---

## CONTRACT

WHAT
What the unit is organised around: one primary org, the directory its knowledge page lands in. It
answers "what would I look up to find this again".

ORGS
- entities: a named thing with identity (person, service, system, device).
- trajectories: a goal or plan whose state changes over time.
- experiences: one event and what it proved (failure, diagnosis, outcome).
- tools: a callable capability and where it worked or failed (command, API, app).
- projects: sustained work with a goal and a deliverable (repository, migration).

SELECTION
- Identity outside this task -> entities.
- State that changes over time -> trajectories.
- Happened once, with a lesson -> experiences.
- A capability you invoke -> tools.
- Ongoing work with a deliverable -> projects.
When two fit, take the object you would search for after this session.

OBJECTS
- "type:name" in the singular ("tools:Docker", "projects:minir-memory"); several allowed. The
  primary org is not repeated.

RELATIONS
- Only units from this window: supersedes, caused_by, part_of, contradicts. A target outside the
  window cannot be resolved and is dropped.

BOUNDARIES
- One primary org; two means two units or one wrong choice.
- Never duplicate content under two orgs for findability.

## REVIEW CHECKS
- An experience recorded as a trajectory.
- A tool lesson recorded as a project.
- objects missing "type:" or using a plural.
- relations referencing a unit outside this window.
- The same content under two orgs.
- The org answers the category instead of the object.
