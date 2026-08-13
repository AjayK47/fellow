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
- keep dashboard authentication enabled;
- configure `TODA_LOCAL_ADMIN_TOKEN` or Slack OpenID login;
- configure `TODA_API_KEYS` for remote event ingestion;
- store credentials in a secrets manager;
- restrict filesystem and backup access;
- keep `TODA_RUN_CODE_ENABLED`, browser control, and Pi coding disabled unless
  the deployment has an appropriate isolation boundary;
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
| Direct ingestion | Localhost or `Authorization: Bearer` using `TODA_API_KEYS` |
| Console to API | Signed session established by Slack OpenID or local admin token |
| Evidence to model | Explicit untrusted-content framing and redaction |
| Memory to requester | Principal, scope, purpose, and destination authorization |
| Model to tool | Capability filtering, schema validation, policy, risk review |
| Consequential action | Human approval when policy or risk requires it |
| Task to worker | Transactional leases, ownership checks, retry and dead lettering |
| Agent to destination | Authorization rechecked immediately before delivery |

## High-risk optional capabilities

`run_code`, browser/CDP access, connected-app writes, and Pi coding workers
materially increase the prompt-injection, exfiltration, and host-compromise
surface. They are not required for the deterministic demo. Enable them only
after reviewing their policies and isolation model.

## Secrets

Never commit `config.json`, database files, task workspaces, logs, or connector
payloads. Run the repository's release verifier and secret scanner before
every release. Rotating a leaked credential is mandatory even if Git history
is later rewritten.
