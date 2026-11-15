# Postmortem: checkout outage on 18 November 2026

**Authors:** Ines Carvalho, Raj Malhotra
**Status:** Final
**Severity:** SEV-2

## Summary
From 13:02 to 13:47 UTC, 31% of checkout requests failed with HTTP 503. The
`checkout-api` pods in the `payments` namespace ran out of memory after release
v4.18.0 doubled the cart cache size to 2 GiB.

## Timeline (UTC)
- 13:02 – Alert `CheckoutErrorRate` fired in #payments-oncall
- 13:10 – Raj rolled back to v4.17.3
- 13:47 – Error rate back below 0.1%

## Action items
- Ines: add a memory limit test to the release checklist (JIRA PAY-1182)
- Raj: lower the default cache size to 512 MiB (PAY-1183)
