# Security policy

## Reporting a vulnerability

Use GitHub private vulnerability reporting on this repository (Security tab → "Report a
vulnerability"). Don't open a public issue. Expect an acknowledgement within 7 days.

## Scope notes

The database holds a whole personal archive: the owner's chat messages and those of everyone they
talked to, plus location, spending, listening and coding history. The API and the MCP server
authenticate nobody and publish on loopback only, and `/tally` runs agent-written SQL under a
SELECT-only role. Report any way to reach either service from beyond the host that runs it, any
path out of that role, and any path where message text, a source's credential or a private
identifier can leak into logs, images or files.

## Supported versions

Only the latest commit on `main`.
