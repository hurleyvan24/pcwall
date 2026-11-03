# Security policy

pcwall is a self-hosted family wall display. It is designed to run on a trusted
home network and to be operated by the household that installs it. This document
explains how to report a vulnerability and what the project treats as a security
boundary.

## Supported versions

Security fixes land on the `main` branch and in the most recent release. Older
releases do not receive backported fixes; please upgrade before reporting.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

1. **Preferred:** use GitHub's private vulnerability reporting. Open the
   repository's **Security** tab and choose **Report a vulnerability**, or go
   directly to <https://github.com/pcwall/pcwall/security/advisories/new>.
   The report is visible only to the maintainers until a fix is published.
2. **Fallback:** if private reporting is unavailable to you, open a GitHub
   issue titled `Security: request private contact` that contains **no
   details** of the problem. A maintainer will reply with a private channel.

Include the version or commit you tested, steps to reproduce, the impact you
observed, and any proof-of-concept material. Please give us a reasonable time to
investigate before sharing details publicly.

### What to expect

- Acknowledgement within 7 days.
- An initial assessment, severity and a proposed timeline within 14 days.
- A fix or mitigation targeted within 90 days of the report, sooner for
  critical issues. We will coordinate the disclosure date with you.
- Credit in the release notes if you would like it.

## Scope and security boundaries

pcwall has three access roles, enforced by the server:

- **Display** (kids, shared wall): read-only views and routine check-offs on
  the wall itself.
- **Parent:** actions guarded by the parent PIN. The PIN is stored only as a
  PBKDF2-SHA256 hash in the wall's data document; the plaintext in
  `config/family.json` is ignored after the first run.
- **Agent:** automation that presents the shared key from `PCWALL_KEY` in
  `.env`. Leaving the key empty disables the role.

Reports in scope include, for example:

- bypassing the parent PIN or the agent key, or escalating between roles;
- reading or writing the wall document, household configuration, tokens or
  credentials through the HTTP API, the MCP server or served static files
  (path traversal, injection, unsafe deserialisation);
- leaking secrets, OAuth tokens or household data in responses, logs or
  exported files;
- cross-site scripting or request forgery in the wall's web client.

Out of scope:

- deployments that expose the service directly to the internet without a
  reverse proxy, TLS and network-level access control. pcwall assumes a
  trusted LAN;
- denial of service by a device already on the trusted network;
- weaknesses in third-party services the wall reads from (calendar, photos,
  weather, news feeds) or in their client libraries;
- issues that require physical access to the display or the host;
- findings from automated scanners without a demonstrated impact.

## Secrets and private data

- `.env`, `credentials.json`, OAuth token caches, `config/family.json` and the
  `data/` directory are ignored by git. Never commit them; the published tree
  ships only `.env.example` and `config/family.example.json`.
- `python tools/sanitize_check.py` scans the tracked tree for household
  details and secret shapes. CI runs it on every push and pull request and
  fails the build on any hit.
- If you believe a secret has been committed, rotate it first, then report
  through the channel above so the history can be cleaned.

Thank you for helping keep family displays safe.
