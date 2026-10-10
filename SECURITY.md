# Security notes

- Never commit `.env`, `data/`, certificates, database files, SMTP passwords, or exported ATAK packages.
- Publish only ports you actually use and place the web services behind DSM reverse proxy with HTTPS.
- Use a dedicated non-admin DSM account for routine file access.
- Back up `data/` before upgrades.
- The registration portal stores pending passwords encrypted and removes them after a decision.
- Review upstream OpenTAKServer release notes before upgrading production deployments.


## Known limitations

Administrator 2FA interoperability is not verified in this release. Do not assume the portal inherits upstream 2FA. Limit administrator access to a trusted network until authentication is verified. Mutable upstream image tags require review before upgrades. Report vulnerabilities privately to the maintainer through GitHub; do not post credentials or user data in public Issues.
