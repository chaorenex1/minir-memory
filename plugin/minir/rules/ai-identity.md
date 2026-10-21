---
id: ai-identity
version: 1
applies_to: [base:B2, dialectic, card_review]
slot: fields
max_tokens: 450
sections: [contract, review_checks]
---

## CONTRACT

WHAT
Who the assistant is in this profile and what it may do. Seven hard fields the profile declares,
six soft fields the operator describes.

HARD FIELDS
- id, name, version, type, system, creator, maintainer: one value each, at most 40 characters.
- Each value must appear verbatim in plugin.yaml; code checks it as a substring of that file.
  Absent there means empty here. Never reconstruct a hard field from memory or from the window.

SOFT FIELDS
- role_duties: what this assistant is responsible for.
- domains_level: domains and the depth reached in each.
- persona_style: voice and manner the operator asked for.
- communication_style: how it should phrase and structure answers.
- permissions: what it may do. Restate operator text; never widen it.
- prohibitions: what it must not do. Restate operator text; never narrow it.

SOURCES
- _sources lists the file paths supplied to this pass, in the order read. A path that was not
  supplied must not appear.

PROVENANCE
- A front-matter `source` of review marks the periodic review's conclusion: same rank as a
  previous card, never operator authority. Operator and operator_edit are declarations.
- A reviewed line never overrides an operator-declared line.

BOUNDARIES
- No tools, completed work, session observations or user traits.
- No permission, capability or prohibition the operator did not write; an invented version
  number is worse than an empty field.

## REVIEW CHECKS
- A hard field cannot be found verbatim in plugin.yaml.
- A permission or prohibition was added that the operator never wrote.
- A session observation (a tool used, work done) was written into the identity.
- _sources is empty, or names a file the pass was not given.
- A review-sourced line is used as if the operator had declared it.
- A hard field carries more than one value or exceeds 40 characters.
