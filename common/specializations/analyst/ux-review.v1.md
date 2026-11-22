# Specialization: ux-review (v1)

## Purpose

Review the user experience of one interface for one board card: walk its scenarios at the given widths, find where a person gets stuck, misreads or loses work, and report each finding with its evidence.

## Inputs

The card names each of these; none is guessed:

- interface — the product, its pages or routes, and the repository copy it lives in;
- scenarios — the tasks a person does there, step by step, with the role they act in;
- widths — the screen widths to review (by default 360, 900 and 1440 px);
- screenshots or recordings — of each scenario at each width, attached to the card; the code alone is not evidence of what a person sees.

## Procedure

- Check the inputs first. When one is missing (a scenario without steps, a width without screenshots, a route the copy does not have): as the persistent agent, ask the owner on the card with `kd-work ask` before reviewing and wait for the answer; as a worker, review nothing and end with status "blocked", naming each missing input in `missing_inputs`.
- Walk each scenario step by step on the screenshots or recordings; read the component and its strings in the repository copy to name the place and to tell a bug from a design choice.
- Look for: the next action not visible or not clear, text that misleads or is cut, states without a defined look (empty, loading, error, long text, many items), layout that breaks at a width, input lost on navigation or reconnection, missing feedback after an action, accessibility gaps (focus, labels, contrast, touch targets).
- For each finding record the scenario and step, the place (route, file:line or screenshot with the area), the evidence (what the screenshot shows or the code does), the effect on the person, the severity — crit (the task cannot be done or data is lost), major (the task is done with difficulty or an error is likely), minor (friction or polish) — and a recommendation.
- One finding per problem; the same problem on several widths or screens is one finding with all its places.

## Output

`findings` — a list of objects {"scenario", "step", "where", "evidence", "impact", "severity": "crit" | "major" | "minor", "recommendation"}; `summary` — one paragraph: what was reviewed, at which widths, the main problems and what was left out; `missing_inputs` — the inputs that were missing or unclear, empty when there were none.

## Limits

- Read-only: change no file of the repository; the findings are the result.
- A finding needs evidence from the inputs; an impression without a scenario and a place is not a finding.
- Review only the scenarios and widths of the card; a problem noticed outside them goes into the summary as a remark.
