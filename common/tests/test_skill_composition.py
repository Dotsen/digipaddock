import pytest

from kd import persistent_skills, specializations as sp, worker_packs


@pytest.mark.parametrize(
    "skill,specs",
    [
        ("stateful-testing", ("implementer", "test-author", "reviewer", "tester")),
        ("localization", ("site-copy", "docs", "implementer", "reviewer", "tester")),
        ("e2e-testing", ("implementer", "test-author", "reviewer", "tester")),
    ],
)
def test_professional_skill_has_the_same_selection_and_provenance_in_both_runtimes(
    skill, specs
):
    registry = sp.load_registry(require_root_owner=False)
    root = sp.SPECS_ROOT.parents[1]
    for name in specs:
        pack = worker_packs.build_prompt(
            registry,
            name,
            [skill],
            card="KD-1",
            brief="Check the task contract",
            agent_type="developer",
        )
        spec, selected = persistent_skills.selection(
            root, "dev", ["spec:" + name, "skill:" + skill], require_root_owner=False
        )
        assert pack.provenance() == sp.provenance(spec, selected)
        assert selected[0].text in pack.system
        assert pack.system.index(spec.text) < pack.system.index(selected[0].text)
    with pytest.raises(sp.SpecError, match="compatible"):
        sp.selected_skills(registry, registry.specs["ui-design"], [skill])
