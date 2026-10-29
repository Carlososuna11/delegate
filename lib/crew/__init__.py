"""Crew: turn a dictation into briefs for remote coding agents.

Phase 1 lives in this repo as a client of delegate. The behaviour contract is
`crew/contract.md` (plain text, not code); the brief builder is pure and does
not touch ssh or the filesystem beyond reading that contract.

Design: docs/specs/2026-10-29-crew-design.md.
"""
