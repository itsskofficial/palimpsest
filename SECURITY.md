# Security

palimpsest reads and writes your notes, so a security bug here is a bug in who can change
them. Reports are welcome and taken seriously.

## Reporting a vulnerability

Please report privately through GitHub:
**[Report a vulnerability](https://github.com/itsskofficial/palimpsest/security/advisories/new)**.

Do not open a public issue for a vulnerability. Include what you found, how to reproduce
it, and what an attacker could do with it. You should hear back within a week.

## Supported versions

Fixes land in the latest release. There are no long-term support branches before 1.0.

| Version | Supported |
|---|---|
| 0.2.x | yes |
| < 0.2 | no |

## What is in scope

The properties below are the ones the project promises. A way around any of them is a
vulnerability. [docs/safety.md](docs/safety.md) describes each one and the test that
enforces it.

- **One write door.** Only `notion/apply.py` writes to a workspace.
- **Contradictions are never applied automatically**, at any autonomy level.
- **The Telegram allowlist.** An unpaired chat must not be able to capture, ask or approve.
- **The API key.** On a non-local bind, every route except the health checks requires
  `PALIMPSEST_API_KEY`, and the server refuses to start without one.
- **Settings are local-only.** Credentials cannot be written over a network interface.
- **Paths stay inside their roots.** Upload filenames and archive keys must not escape
  their directories.
- **Captured content is data.** Text inside a source must not be able to instruct the
  agent.

## Out of scope

- Anything that requires an attacker to already have the machine, the config file, or the
  API key.
- The unsigned installers warning on first launch. That is known and on the
  [roadmap](docs/roadmap.md).
- Findings in dependencies with no demonstrated path through palimpsest.
