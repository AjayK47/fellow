---
name: toda-skill-authoring
display_name: Toda Skill Authoring
description: Author, validate, and safely package a Toda skill.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: builtin
platform_key: builtin.toda.skill_authoring
activation: {mode: optional}
required_capabilities: {all_of: [workspace.write]}
warnings:
  - A skill body that matches the static security scanner is rejected outright for builtin/connector packages, and blocks every organization's skill sync for a marketplace package with a high-severity finding -- there is no warning-only mode for a high-severity match.
  - Never quote a dangerous command or a real secret pattern verbatim inside a skill body to illustrate what to avoid -- describe it in words; the scanner matches text, not intent.
steps:
  - order: 1
    name: Contract
    description: Decide which of Toda's two skill mechanisms applies, then define the goal, activation, required capabilities, steps, and warnings.
  - order: 2
    name: Package
    description: Write valid frontmatter and body matching the exact schema, sized and structured to survive the loader.
  - order: 3
    name: Validate
    description: Self-check against the schema and the security rules before proposing the skill be added or approved.
---
# Toda Skill Authoring

Toda has two separate mechanisms for a skill to exist. Pick the right one
before writing anything.

## Which mechanism applies

**Runtime org skill proposal** (`skill_propose_create` /
`skill_propose_update`) -- available to any agent in a live conversation.
Takes a simple record: `name`, `description`, `goal`, `steps`, `inputs`,
`preconditions`, `warnings`. No YAML frontmatter, no security scanner, no
file involved. Every call creates a draft; it has no effect until a human
reviews and approves it, and it never silently becomes active. This is the
right mechanism when the task is "this org has a reusable procedure worth
remembering" discovered during ordinary work.

**File-backed skill package** (a `SKILL.md` file under
`builtin_skills/<name>/`, validated by `SkillPackageCatalog`) -- this is the
format of the file you are reading right now. It is source, not runtime
state: shipping a new one, or editing an existing one, means committing a
file to the codebase (or, for a `marketplace`-kind package, to whatever
additional package root a deployment has configured) so it gets picked up
the next time the catalog loads. `write_file` lets you draft this
file's content for a human to review and commit; it does not, by itself,
register the skill anywhere a running agent will see it. The rest of this
skill covers this format.

## The manifest schema

The frontmatter is YAML between a first line of exactly `---` and a
subsequent line starting with `---`, followed by a non-empty body. The
backing model rejects any key not in this exact list -- there is no
tolerance for an extra field, however reasonable it looks:

`name`, `display_name`, `description`, `version`, `license`, `origin`,
`kind`, `platform_key`, `activation`, `required_capabilities`,
`response_validation`, `warnings`, `steps`, `inputs`, `preconditions`,
`tags`, `security_verdict`, `security_findings`.

Key fields:

- **`name`** -- lowercase, digits, dots, hyphens, underscores only, starting
  with a letter or digit, 64 characters max. This becomes the skill's unique
  id across every configured package root; a name collision fails the whole
  catalog load, not just this one file.
- **`kind`** -- one of `builtin`, `connector`, or `marketplace`. `builtin`
  and `connector` are treated as immutable, reviewed, shipped-with-the-code
  procedures -- this is what every file in this directory is. `marketplace`
  is for a less-trusted, separately-sourced package; it is the only kind
  that tolerates a scanner finding without failing to load at all (see
  Security below), and it is still far from a free pass.
- **`platform_key`** -- the stable identifier a running deployment uses to
  materialize this package into each organization's skill index across
  version bumps. Don't change it on an existing skill without understanding
  what currently depends on it.
- **`activation`** -- `mode` (`optional` or `mandatory`) plus optional
  `destination_types`, `trigger_kinds`, and `purposes` that narrow when this
  skill is even considered for injection into a running agent's context.
  `mandatory` skills are always active; don't mark something mandatory
  unless every conversation genuinely needs it in scope.
- **`required_capabilities`** -- `all_of` and/or `any_of` lists of
  capability ids (the same ids tool definitions declare, like
  `workspace.read` or `task.task_delegate`). A skill missing an
  `all_of` capability, or with no member of `any_of` present, is reported as
  needing setup rather than silently offered with instructions the agent
  can't actually follow.
- **`response_validation`** -- optional `enabled` flag plus `instructions`.
  When enabled, a separate model checks the agent's final response against
  these instructions (and the skill's own `steps`/`warnings`) and can flag
  violations or suggest a correction. Only turn this on for a skill where a
  wrong final answer is genuinely worth a second model pass to catch --
  it's not free.
- **`steps`** -- ordered `{order, name, description}` objects. Each should
  describe a checkable stage of the work, not just restate the skill's
  title in three pieces.
- **`warnings`** -- short, specific cautions surfaced directly alongside the
  skill's instructions. A generic warning that would apply to any skill
  ("be careful") doesn't earn its place here.
- **`origin`** -- if this package started from another source, record it
  here (project, adapted-from, source commit or similar) and leave it
  alone on later edits unless the provenance actually changed.

Size and structure limits enforced by the loader: body under roughly 256,000
bytes, the whole raw file (frontmatter plus body) not much larger than that,
no symlinked `SKILL.md` files, and the file must start with the opening
fence at the very first byte -- no leading blank line, no byte-order mark.

## Security scanning

Before a package loads, its body text is scanned for five categories of
content, described here in words rather than as literal examples so this
file itself doesn't trip the same scanner while explaining it:

1. **Instruction-override language** -- phrasing that claims to cancel or
   supersede the agent's prior instructions, or that asks it to disclose
   its system prompt.
2. **Mass-destructive commands** -- a recursive forced filesystem delete, a
   disk-format command, a raw low-level disk write, or an unqualified
   database-wide delete or drop.
3. **Inline executable/shell-out code** -- a call that shells out to the
   operating system, that spawns a subprocess, that dynamically evaluates a
   string as code, or that pipes a downloaded script straight into a shell
   interpreter.
4. **Secret material** -- the literal name of a well-known provider API-key
   or bot-token environment variable, an AWS-style secret access key
   reference, or a private-key file header banner.
5. **Outbound data exfiltration code** -- a snippet that issues a write-style
   HTTP call (the kind that sends a request body, not a plain read) to an
   arbitrary external host from inside the skill body.

A relative path that climbs above its own starting directory is flagged
separately as a path-traversal finding.

**Consequences differ by `kind`:** for `builtin` or `connector`, any finding
at all fails the file's load entirely -- the whole catalog refuses to start
until it's fixed. For `marketplace`, a finding is recorded rather than
failing the load immediately, but a high-severity finding still blocks
every organization's skill sync the moment a deployment tries to
materialize it -- "marketplace" buys a later, more visible failure, not
tolerance.

**Practical rule for authoring:** if the skill's subject matter requires
explaining a dangerous command or a secret-handling pattern (which, for the
software-development skills in this directory, it often does), describe it
in prose rather than writing the literal command, the literal function
call, or the literal secret-variable name. A reader does not need the exact
trigger text to understand what to avoid.

## Procedure

### 1. Contract

Before writing frontmatter:

- State the skill's goal in one sentence -- what should be true after an
  agent follows it that wasn't true before.
- Decide activation: does every relevant conversation need this in context
  (`mandatory`), or only when its trigger conditions are actually present
  (`optional`, the right default for nearly everything)?
- List the real tool capabilities the instructions depend on, split into
  `all_of` (every one is required) and `any_of` (at least one is enough).
  Don't list a capability the instructions don't actually use, and don't
  omit one they silently assume.
- Draft `steps` as checkable stages and `warnings` as specific, non-generic
  cautions.

### 2. Package

Write the frontmatter and body to a draft file with `write_file`,
matching the schema above exactly -- no extra keys, correct `kind`, `name`
matching the regex. Structure the body so an agent unfamiliar with the
subject can follow it: what the skill is for, when to use it and when not
to, the concrete Toda tools involved, a numbered procedure, common
pitfalls, and a closing verification checklist.

### 3. Validate

At minimum, self-check by reading the draft back with `read_file` and
walking it against: does it start with the opening fence at byte zero, does
the frontmatter parse as a mapping with only allowed keys, does `name` match
the pattern, is `kind` one of the three valid values, and -- reading your
own body slowly -- does any sentence resemble one of the five scanner
categories above closely enough that it would likely match, even if the
intent was educational rather than malicious.

If `task_delegate` is available, get a real signal from a Pi child Task instead of just a
self-check: delegate a task that loads the draft file through the actual
catalog loader and reports whether it parses and whether the scanner flags
anything, before proposing the file be committed. A self-review is a good
first pass; an actual load is the only thing that proves the file will
survive the real loader.

Once the draft passes, hand it off for a human to add to the source tree
(or to whatever marketplace root a deployment uses) -- this skill's own
`write_file` step never registers it anywhere on its own.

## Pitfalls

- **Adding a frontmatter key that isn't in the allowed list.** The model
  rejects the whole file, not just the extra key.
- **Quoting the dangerous pattern verbatim "for clarity."** Describing a
  destructive command in prose teaches the same lesson without failing the
  scanner.
- **Marking a skill `mandatory` by default.** That injects it into every
  matching conversation's context whether it's relevant or not -- reserve it
  for skills that genuinely always apply.
- **Listing a capability the instructions don't use, or omitting one they
  do.** Both produce a skill that's either gated when it shouldn't be, or
  offered when the agent can't actually follow it.
- **Assuming `write_file` registers the skill.** It only drafts the
  file; getting it into the running catalog is a separate, human-reviewed
  step.
- **Treating `marketplace` kind as a way around the scanner.** It changes
  when the failure happens, not whether a high-severity finding is
  eventually fatal.

## Verification checklist

- [ ] The right mechanism was chosen (runtime proposal vs. file-backed
      package) before writing anything
- [ ] Frontmatter contains only allowed keys, with a valid `name` and `kind`
- [ ] `required_capabilities` matches what the instructions actually depend
      on, no more and no less
- [ ] No sentence in the body quotes a dangerous command or a secret
      pattern verbatim
- [ ] The draft was read back and checked against the schema before handoff
- [ ] If `task_delegate` was available, the draft was actually run
      through the loader rather than only self-reviewed
- [ ] The handoff states plainly that a human still needs to commit or
      approve the package
