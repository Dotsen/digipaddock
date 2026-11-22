# Skill: python-backend (v1)

- Prefer a functional core with explicit inputs; keep filesystem, process, clock and network effects at narrow boundaries that tests can replace.
- Type hints on new public functions; no `shell=True`, `eval`, or catch-all handlers that turn failures into success.
- Validate untrusted input before effects and fail closed on unknown fields, paths and statuses.
- Tests: pytest, observable behavior and refusals. Follow the repository's change/risk policy (digipaddock: docs/testing.md); run affected tests and consumers, and full Linux regression for broad risk or unknown impact.
