# Data protection and GDPR deployment checklist

The software provides privacy and security controls, but installing it does not by itself
make an organisation GDPR compliant. The controller remains responsible for lawful
purposes, deployment configuration, rights handling, processors and transfers, retention,
and demonstrating that the controls remain effective.

## Before processing personal data

1. Identify the controller and, when applicable, DPO; configure `privacyControllerName`,
   `privacyControllerContact`, `privacyDpoContact`, and `privacyLawfulBasis`.
2. Decide whether station measurements or coordinates identify a person, household, or
   workplace. Apply data minimisation and use `restricted` station policy by default when
   in doubt. Document a lawful basis for every purpose and dataset.
3. Keep a record of processing activities: data subjects/categories, purposes, legal
   basis, recipients/processors, EEA transfer safeguards, retention, and security.
4. Complete a DPIA before processing likely to create high risk, including systematic
   monitoring or precise location/environmental data linked to individuals.
5. Use HTTPS, a private writable database, strong administrator credentials, least
   privilege, encrypted backups, restricted logs, and tested restoration.
6. Inventory SMTP, map-tile, hosting/CDN, backup, InfluxDB, and other providers. Put
   Article 28 terms in place with processors and document international transfers.
   Self-host browser assets where disclosing visitor IP addresses is unnecessary.
7. Approve concrete deletion schedules. The configured periods are displayed to users,
   but deletion is not automatic because legal holds, security evidence, backups, and
   data owned by the separate collector require controller decisions.

## Data-subject requests

Authenticated users can download a JSON copy from **Profile → Download my personal
data**. Verify identity before fulfilling rectification, erasure, restriction, objection,
or other requests. Search the auth database, email queue, backups, InfluxDB, and collector
CSV storage as applicable. Record the request, decision, scope, dates, and any exception.

The export excludes password/token hashes and the session secret. API-token metadata and
the data subject's audit events are included.

## Retention and deletion runbook

- Review rejected/abandoned account requests and expired single-use tokens promptly.
- Remove sent email content after the approved troubleshooting period.
- Review access/impersonation events after `privacyAuditRetentionDays`.
- For valid erasure, deactivate immediately, revoke tokens, remove email, permissions,
  workflows and notifications, and address mail queues and backups. Preserve or
  pseudonymise only records covered by a documented exception.
- Apply the collector's retention schedule to CSV data and InfluxDB. This application
  reads but does not own or delete those stores.

## Incidents and accountability

Maintain an incident register and tested response procedure. Assess and document every
personal-data breach; notify the competent authority without undue delay and where
feasible within 72 hours when required, and affected people when the high-risk threshold
is met. Periodically test access control, restore, rights handling, dependency updates,
and retention. Revisit the notice and DPIA when processing or risk changes.
