# Security

## Reporting

Email security@milux.co.uk, or matt@milux.co.uk, with the version (`VERSION`), what you
found and how to reproduce it. Please do not open a public issue for a vulnerability. You will
get an acknowledgement within five working days.

## Supported versions

The newest tagged release only. Pinecone is pre-1.0 and every release may change behaviour.

## What Pinecone handles

Position history is personal data about identifiable people. Pinecone reads it from a TAK
Server's database, keeps it on the machine that pulled it, and sends it nowhere. The
database credential is read on the box and never printed or logged. The server binds
loopback by default and has no authentication; exposing it beyond loopback is an operator
decision and the server says so when asked to.

## Updates

Installed copies update only from tagged releases, verified against the published sha256,
never from a branch.
