# Release process

1. Confirm every built-in skill has a reviewed SPDX-style license and origin.
2. Run `python scripts/verify_release.py`.
3. Run the full Python suite and console production build.
4. Run secret, dependency, CodeQL, and container scans.
5. Build the Python wheel and container from a clean checkout.
6. Install the wheel and start the container in fresh environments.
7. Test the deterministic quickstart, local login, Slack ingress, approval,
   delivery, backup, and restore paths.
8. Update `CHANGELOG.md`, tag `vX.Y.Z`, and let the release workflow publish
   attestable artifacts.

The PyPI distribution is `toda-ai`; the Python import and executable are
`toda`. Publishing requires configured trusted publishing for this repository.
