# Explicit release verification

The two procedures are different specializations of existing agents. Their implementation does not introduce automatic deployment or a mandatory gate for every change.

1. On a separate verification task, route `release-check` to an analyst with `repo.read`. Supply the owner's goal, complete diff at full base/head, target environment and the full operations/checks/rollback plan. Record the plan hash. The result is ready, needs_changes or blocked with evidence; it grants no approval.
2. The existing installation flow obtains its own required authorization. Do not deploy just to test a specialization.
3. Route `post-deploy` to the persistent sysadmin with `host.read` and `servers.read`. Supply the expected version, performed operations and required checks. Verify actual version, services, health, bounded logs and the affected user scenario. Passed, failed and blocked are distinct; a green health endpoint alone is insufficient.
4. A changed commit, target or plan requires a new applicable conclusion. Preserve the old one as historical. Repairs and rollback use a separate authorized action, not the verification role.

For a first real probe use one independently approved installation on the factory's host. Attach the preflight basis and findings, performed operation/version, postflight evidence and the owner's acceptance. Real installation evidence is distinct from registry/prompt tests. A sysadmin worker request is refused when its type or slots are unsupported. A supported unattended runner without an essential reader returns blocked and names the missing channel; neither case grants access.

Keep each `kd-task edit --append-notes` argument below its 4000-character limit. Use a compact structured conclusion and separate short evidence notes if needed. A failed report write is not a recorded conclusion.

For generated agents, `new_agent.sh --check` validates the proposed inputs; it does not prove that the installed files match them. Use `new_agent.sh --verify <role>` for that read-only comparison, requiring exit 0 after installation and after any rollback. Exit 3 means drift, not a successful check. Reinstall through the normal authorized workflow only when needed.

Checklists for the probe:

- [x] A known migration/configuration/rollback omission is found with its failure scenario.
- [x] The corrected plan is reviewed at its new hash.
- [x] Installed version matches the expected commit on the named target.
- [x] Services, health, relevant logs and the affected feature are checked.
- [x] Wrong version and missing essential evidence cannot produce passed.
- [x] No deployment, repair, rollback or trust change is performed by these procedures.

A check's conclusion describes the exact installation it observed, with its stated evidence limits; it does not apply to a later commit or plan.
