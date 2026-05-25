# ADR-001: Use httpx instead of Selenium

**Date:** 2026-05-24
**Status:** Accepted

## Context
The original scraper used Selenium to drive a browser through the SOS stateful form flow.
This required a Chrome driver, was slow, brittle, and hard to run in CI.

## Decision
Replace Selenium with httpx. The SOS site's session can be established purely via HTTP:
GET `getElectionDetails.do` → POST `getElectionEVDates.do` → POST `getEVDetails.do`,
carrying the `JSESSIONID` cookie forward. No browser needed.

## Consequences
- Faster (~10x), CI-friendly, no Chrome dependency
- Must maintain cookie jar manually across requests (httpx.Client handles this)
- Cold POSTs without session establishment will fail — session must always be walked
