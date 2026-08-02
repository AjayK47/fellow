---
name: docket-skill-authoring
display_name: Docket Skill Authoring
description: Author, validate, and safely package a Docket skill.
kind: builtin
platform_key: builtin.docket.skill_authoring
activation: {mode: optional}
required_capabilities: {all_of: [workspace.write]}
steps: [{order: 1, name: Contract, description: Define trigger, inputs, steps, and evidence.}, {order: 2, name: Package, description: Write valid frontmatter and instructions.}, {order: 3, name: Validate, description: Check safety, readiness, and examples.}]
---
Define the skill's goal, activation, required capabilities, warnings, response contract, and completion evidence. Keep instructions focused, scan for prompt injection, compute a content hash, and test the package before cataloging it.
