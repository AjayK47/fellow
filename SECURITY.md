# Security Policy

Toda handles organizational conversations, credentials, generated artifacts,
and actions against external systems. Treat every deployment and backup as
sensitive infrastructure.

## Supported versions

Security fixes are provided for the latest published minor release. Until the
project reaches 1.0, releases are alpha software and may contain breaking
changes.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for the Toda repository. If that
feature is unavailable, contact the maintainer privately through the contact
method on their GitHub profile. Do not open a public issue containing an
exploit, credential, private company data, or tenant-isolation details.

Include the affected version, deployment shape, reproduction steps, impact,
and any suggested mitigation. You should receive an acknowledgement within
three business days.

## Deployment boundary

The community release is designed for a self-hosted organization. The default
Compose mapping listens only on `127.0.0.1`. Before exposing Toda to a network:

- terminate TLS at a maintained reverse proxy;
- keep `dashboard.auth_disabled` false, and give every operator their own
  dashboard account rather than sharing the owner login `toda init` creates;
- set `server.api_keys` for remote event ingestion (localhost stays keyless);
- store credentials in a secrets manager, and keep `config.json` out of Git;
- restrict filesystem and backup access -- the SQLite file contains every
  memory, transcript, artifact and credential the deployment holds;
- keep `tools.run_code_enabled`, `tools.browser_enabled` and
  `agent.pi_coding_enabled` false unless the host itself is expendable
  (see below -- none of them is sandboxed);
- test backup restoration and retention;
- monitor failed jobs, approval activity, and authorization suppressions.

Toda refuses network-bound startup when dashboard authentication is disabled
or automatic write approval is enabled. This guard is not a substitute for a
firewall, TLS, or correct reverse-proxy configuration.

## Trust boundaries

| Boundary | Control |
|---|---|
| Slack to gateway | HMAC/timestamp verification or authenticated Socket Mode |
| Connector to gateway | Provider webhook signature verification |
| Direct ingestion | Localhost, or `Authorization: Bearer` using `server.api_keys` |
| Console to API | Signed session cookie from a dashboard account (email + password) |
| Console user to workspace | `organization_id` bound to the session, never accepted from the request |
| Console user to principal | Acting principal derived from the session; it is not a request parameter |
| Console user to conversation | Sessions, Tasks and their transcripts are readable by their participants; owner/admin may read any, and that read is audited |
| Claim to participation | Recorded only from a platform event the provider authorized, or a conversation the caller started; never from a caller naming one |
| Evidence to model | Redaction, plus untrusted-content framing on every tool Toda did not author |
| Memory to requester | Principal, scope, purpose, and destination authorization |
| Model to tool | Capability filtering, schema validation, policy, risk review |
| Contained action | Runs directly in every mode: Toda's own memory, artifacts, plans, workspace, schedule and Tasks |
| Outbound action | Automated risk review by default (`autonomous`); a human only in `supervised`/`restricted`, or on escalation |
| Task to worker | Transactional leases, ownership checks, retry and dead lettering |
| Agent to destination | Authorization rechecked immediately before delivery |

## Who approves what

Under the default `autonomous` tool-policy mode, an auxiliary model -- not a
person -- is the last check before an outbound write, including a
destructive or financial connector action. It may approve, deny, or escalate
to a human. Choose `supervised` or `restricted` if a person must see every
outbound action first.

Contained actions (Toda's own memory, artifacts, plans, workspace, schedule
and Tasks) run directly in every mode. They are reversible and internal, and
gating them made approvals fire so often that the ones that mattered stopped
being read. A per-tool override in `tool_policies` can still gate any of them.

## High-risk optional capabilities

`run_code`, browser/CDP access, connected-app writes, and Pi coding workers
materially increase the prompt-injection, exfiltration, and host-compromise
surface. They are not required for the deterministic demo, and all default
to off.

**`run_code` is not sandboxed.** Despite the `RunCodeSandbox` class name, it
launches an ordinary Python subprocess under Toda's own operating-system
account. It constrains the working directory, the environment (secrets are
stripped), which Toda tools may be called back into (read-risk only), wall
time and output size. It does **not** constrain absolute filesystem paths,
the network, other processes, or resource usage. A script can read
`/etc/hosts`, list the operator's home directory, open a socket to any host,
and read the Toda database file directly -- this was verified, not assumed.

Approval is not containment. An action reviewer judging a script "safe" is a
judgment about intent, not a limit on capability, and the script is written
by a model that may be acting on injected instructions. Enable `run_code`
only where the host account itself is expendable -- a disposable container
with no credentials, no database mount and no network egress.

The same reasoning applies to `agent.pi_coding_enabled`, which runs the `pi`
CLI with network access. With the default `agent.pi_isolation_required: true`,
Toda refuses to launch it until `agent.pi_execution_user` names a dedicated
low-privilege Unix account. That account and the separate agent workspace keep
the control-plane files private by filesystem permissions, but they are still
not a VM/container boundary: absolute paths and network access remain available
to that account. Routine Pi launches do not pause for a human; hazardous
launches can be reviewed or escalated. `tools.browser_enabled` likewise drives
a real browser against live sites carrying whatever credentials that browser
holds.

## Secrets

Never commit `config.json`, database files, task workspaces, logs, or connector
payloads. Run the repository's release verifier and secret scanner before
every release. Rotating a leaked credential is mandatory even if Git history
is later rewritten.
