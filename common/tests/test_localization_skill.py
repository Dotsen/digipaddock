import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "localization_check", Path(__file__).parents[2] / "scripts/check_localization.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_missing_key_and_renamed_parameter_are_detected_and_fixed():
    catalogs = {
        "en": {"action": "Open {name}", "count": "Selected: {count}"},
        "ru": {"action": "Open {other}"},
    }
    issues = checker.check(catalogs)
    assert any("missing key" in i and "count" in i for i in issues)
    assert any("mismatched placeholders" in i and "action" in i for i in issues)
    catalogs["ru"] = {"action": "{name}: open", "count": "{count} selected"}
    assert checker.check(catalogs) == []


def test_unsupported_plural_syntax_is_not_a_false_pass():
    catalogs = {
        "en": {"count": "{count, plural, one {item} other {items}}"},
        "ru": {"count": "{count} items"},
    }
    assert any("ICU syntax is unsupported" in i for i in checker.check(catalogs))


def test_real_catalog_parity(capsys):
    assert checker.main([]) == 0
    assert "visual checks remain separate" in capsys.readouterr().out
