# Skill: security-review (v1)

- Look for trust-boundary crossings: untrusted text reaching a shell, a path, a prompt boundary, a sudo or ACL decision.
- Check symlink and TOCTOU handling on files written by root or by another user; check that refusals are not swallowed.
- Check that no change widens permissions, sandbox, network domains, secrets access or approval flow without an explicit owner decision.
- Report the concrete failure scenario for each finding: input, state, wrong outcome.
