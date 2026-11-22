"""Check the cabinet's flat catalogs and simple placeholders, without running the UI."""

import argparse
import json
from pathlib import Path
import re

PARAM = re.compile(r"\{([A-Za-z0-9_]+)\}")
ICU = re.compile(r"\{[^{}]+,\s*(?:plural|select|selectordinal)\s*,")
ROOT = Path(__file__).resolve().parents[1]


def check(catalogs):
    issues = []
    if len(catalogs) < 2:
        raise ValueError("provide at least two language catalogs")
    languages = sorted(catalogs)
    for language, catalog in catalogs.items():
        if not isinstance(catalog, dict):
            raise ValueError(f"{language}: expected a flat JSON object")
        for key, value in catalog.items():
            if not isinstance(value, str):
                issues.append(f"{language}:{key}: expected text")
            elif ICU.search(value):
                issues.append(
                    f"{language}:{key}: ICU syntax is unsupported by the cabinet interpolator"
                )
    keys = set().union(*(set(c) - {"$schema"} for c in catalogs.values()))
    for key in sorted(keys):
        baseline = None
        for language in languages:
            value = catalogs[language].get(key)
            if value is None:
                issues.append(f"{language}:{key}: missing key")
            elif isinstance(value, str):
                params = set(PARAM.findall(value))
                if baseline is None:
                    baseline = params
                elif params != baseline:
                    issues.append(
                        f"{language}:{key}: mismatched placeholders {sorted(params)}; expected {sorted(baseline)}"
                    )
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalogs", nargs="*", type=Path)
    args = parser.parse_args(argv)
    paths = args.catalogs or [
        ROOT / "common/cabinet/web/messages" / (lang + ".json") for lang in ("ru", "en")
    ]
    if len({p.stem for p in paths}) != len(paths):
        parser.error("catalog filenames must identify distinct languages")
    try:
        issues = check(
            {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in paths}
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for issue in issues:
        print(issue)
    if not issues:
        print(
            "Catalog keys and placeholders match. Semantic, plural and visual checks remain separate."
        )
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
