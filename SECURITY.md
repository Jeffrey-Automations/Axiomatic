# Security policy

## Supported versions

Security fixes are applied on the default branch. This project is
not published as a versioned library. Older snapshots are not supported.

## Reporting a vulnerability

Do not open a public issue, pull request, or discussion for a suspected
vulnerability.

Use either private channel:

- **GitHub Security Advisories.** Open a private report from this
  repository's Security tab, **Report a vulnerability**. This is the
  preferred channel.
- **Email.** `jechenmailman@gmail.com`, with a subject line that begins
  with `[SECURITY]`.

Include the affected version, platform, impact, and steps to reproduce.
Do not attach credentials, tokens, or unredacted logs.

The maintainers aim to acknowledge a report within 5 business days, and
to coordinate a fix and disclosure within 90 days. There is no bug-bounty
promise.

## Operational notes

Commands that change the machine are limited to the owner. Shell commands
for that owner are argument lists passed to PowerShell or ``sh``. Do not
extend that path to input from anyone else.

Keep credentials in gitignored files. Files named as examples are
placeholders. Do not commit tokens. The dashboard serves read-only GET
requests.
