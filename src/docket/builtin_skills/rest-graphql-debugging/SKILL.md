---
name: rest-graphql-debugging
display_name: REST and GraphQL Debugging
description: Diagnose API failures from request, response, schema, and service evidence.
kind: builtin
platform_key: builtin.hermes.rest_graphql_debugging
activation: {mode: optional}
required_capabilities: {all_of: [workspace.read]}
warnings:
  - Docket has no built-in HTTP client with custom headers, auth, or POST/PUT support -- web_fetch only GETs public pages. Any real call against an authenticated or internal API requires a Pi child Task.
  - Never paste a real bearer token, API key, or cookie into a report or into a workspace file -- redact it, even in a reproduction command meant for a vendor bug report.
steps:
  - order: 1
    name: Reproduce
    description: Get the exact failing request and response, captured by an actual call, not assumed from memory.
  - order: 2
    name: Isolate the layer
    description: Walk connectivity, TLS, auth, request format, response parsing, and semantics in order; don't skip a layer.
  - order: 3
    name: Verify
    description: Retest the smallest fix against the same real reproduction and capture the correlation ID for anything unresolved.
---
# REST and GraphQL Debugging

Diagnose an API failure by isolating which layer broke, in order, instead of
guessing at the fix from the symptom alone. A 200 response can hide broken
data; a 500 can mask a one-character auth mistake. Walk the chain in order:

```
1. Connectivity   -- can we reach the host at all?
2. TLS            -- is the certificate valid and trusted?
3. Auth           -- are credentials correct and unexpired?
4. Request format -- does the payload match what the server expects?
5. Response parse -- does the client accept what actually came back?
6. Semantics      -- does the data mean what the caller assumes it means?
```

## Docket's real tool surface for this

There is no built-in HTTP client with header, auth, or write-method support.
`web_fetch` exists, but it is deliberately narrow: it fetches one public page
by URL, read-only, no custom headers or authentication, and its results are
untrusted references you should verify rather than assume are current. It is
useful for reading a vendor's public API documentation; it cannot exercise
the actual failing endpoint if that endpoint needs an auth header, a POST
body, or lives behind a private network.

Making the actual failing call -- with headers, with a body, following
redirects, timing the connection -- requires a Pi child Task: a
full-trust subprocess with real network access, scoped to the shared
workspace, available only when Phase D is enabled for this deployment. This
skill's investigation only produces real evidence when that capability is
available; without it, you're limited to reading whatever request/response
evidence a human has already pasted into the conversation, plus public
documentation via `web_fetch`. Say which situation you're in before
diagnosing.

`workspace_write` is useful for saving a reproduction script or a regression
test once the failure is understood, so it can be re-run later via
`delegate_tasks` rather than re-typed each time.

## When to use

- An API call returns an unexpected status or body.
- Auth is failing (401/403) after what should have been a valid token or key.
- A GraphQL call returns HTTP 200 but the operation didn't actually succeed.
- Pagination, rate limiting, or an integration test needs debugging.

Skip this for UI rendering issues, database query tuning, or DNS/firewall
infrastructure problems that need to be escalated to whoever owns that
infrastructure.

## Step 1 -- Reproduce

Get the exact call and its exact result. If a human already has this
(browser network tab, an error message with a status code), start from that.
Otherwise, delegate a task that makes the real call and reports back:
status code, response headers, and enough of the body to see the shape of
the problem -- with any token or credential redacted in what gets reported
back to you.

**GraphQL-specific:** a request can return HTTP 200 while the operation
itself failed. Always have the delegated call check the response body's
`errors` field regardless of status code -- an empty `errors` list plus
populated data is the only real success signal for GraphQL.

## Step 2 -- Isolate the layer, in order

Don't skip ahead to "it's probably the payload" without ruling out the
earlier layers first -- each one produces symptoms that look like the next.

### Connectivity and timeouts

Can the host be reached at all, and is a timeout a connection problem or a
slow server? Delegate a call that distinguishes "cannot connect" (DNS,
firewall, VPN) from "connects but the server is slow to respond" -- these
need different fixes entirely. Any script written for this should always
pass an explicit timeout; a client with no timeout set can hang indefinitely
on a slow or hung server.

### TLS

Delegate a check of the certificate: expired, self-signed, hostname
mismatch, or a missing trusted root. Never permanently disable certificate
verification as a "fix" -- it's a debug-only workaround if used at all, and
should never survive past the diagnosis.

### Auth

Checklist:
- Is the token actually expired? (decode the expiry claim if it's a JWT)
- Right auth scheme -- bearer token vs basic auth vs a custom header
  vs an API key expected as a query parameter?
- Right environment -- a staging credential used against production (or vice
  versa) is a classic and easy mistake to miss.

### Request format

The most common silent failure here is a mismatch between the declared
content type and what was actually sent -- for example, a caller that builds
a form-encoded body while claiming a JSON content type, or that declares it
accepts one format while the code that parses the response expects another.
Confirm exactly what was sent (the delegated call should report both the
declared headers and the actual serialized body) against what the server's
schema or documentation says it expects.

### Response parsing

Before parsing a response as JSON, check its actual content type. A common
failure is an HTML error page returned where JSON was expected -- parsing
that as JSON produces a confusing downstream error that has nothing to do
with the real problem.

### Semantics

The response parsed cleanly -- but does the data mean what the caller
assumes? Does the returned ID match the one requested? Are timestamps in the
expected timezone? Is pagination actually returning every result, or just
the first page?

## HTTP status quick reference

- **401** -- credential missing, wrong, or expired. Confirm the header is
  actually present in what was sent, not just present in the code that's
  supposed to add it.
- **403** -- authenticated but not authorized: wrong scope, wrong resource
  owner, or an IP allowlist blocking the caller.
- **404** -- check the exact path (trailing slash, API version prefix,
  typo) before assuming the resource doesn't exist.
- **409** -- a state collision: duplicate create, or a stale conditional
  header racing another writer.
- **422** -- valid request shape, invalid data. The error body usually names
  the specific bad field.
- **429** -- rate limited. Respect `Retry-After` and back off with
  increasing delay across retries rather than retrying immediately.
- **5xx** -- generally not the caller's fault. Capture the correlation ID and
  escalate to whoever owns the service; back off and retry for 502/503/504,
  but don't spin retrying a 500 without evidence it's transient.

## Step 3 -- Verify

Retest the smallest possible fix against the same reproduction from Step 1 --
not a new, different call. If it's now correct, capture the before/after
evidence. If it's still broken, that's a new data point for the next
hypothesis, not a reason to try a second simultaneous change.

Always capture the response's correlation/request ID (commonly
`X-Request-Id`, `X-Trace-Id`, or a CDN-specific header) for anything that
ends up needing a vendor bug report -- it's the fastest path to support
actually finding the failing request on their side.

## Security

- Never write a real token, key, or cookie into a workspace file or into a
  report meant for a human or a vendor -- redact it (`Bearer <REDACTED>`)
  even in an otherwise-exact reproduction.
- A 404 on a resource that requires auth to view shouldn't itself reveal
  whether that resource exists for a different account -- note this as a
  finding if you see it, don't just work around it.
- Flag (don't silently accept) a production error response that leaks a
  stack trace, an internal hostname, or a framework version string.

## Pitfalls

- **Assuming the payload is the problem before ruling out auth and TLS.**
  Each earlier layer's failure often looks exactly like a payload issue from
  the outside.
- **Trusting status code alone for GraphQL.** Always check the `errors`
  field regardless of what the HTTP status says.
- **Parsing a response as JSON without checking content type first.**
- **Reporting a fix as verified without re-running the exact original
  reproduction.**
- **Leaving live credentials in a saved reproduction script or a report.**

## Verification checklist

- [ ] The exact failing request and response were captured via a real call
      (or their absence from this run is explicitly noted)
- [ ] Each layer (connectivity, TLS, auth, request format, response parsing,
      semantics) was checked in order, not skipped
- [ ] For GraphQL, the `errors` field was checked regardless of HTTP status
- [ ] The fix was retested against the same original reproduction
- [ ] Correlation/request IDs are captured for anything still unresolved
- [ ] No credential appears unredacted anywhere in the report or in a saved
      workspace file
