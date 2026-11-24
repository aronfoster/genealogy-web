"""Run one invented workshop scenario through the same reader and auth harnesses."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from browser_scenarios.records import build, identifier
from check_browser_suite import WEB_VARIANTS

from genealogy.model import canonical_bytes

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "scripts/browser_scenarios"


def materialize(root: Path, historical: bool) -> Path:
    build(root, historical=historical)
    fixture = ROOT / "tests/fixtures/publication-inputs"
    (root / "roots.json").write_bytes(
        canonical_bytes(
            {
                "roots": [identifier("person", 1, historical=historical)],
                "rationale": "Invented workshop family",
            }
        )
    )
    path = root / "inputs.json"
    path.write_bytes(
        canonical_bytes(
            dict(
                schema_version=1,
                validation_profile="synthetic",
                corpus="corpus",
                build_year=2026,
                family_roots="roots.json",
                historical_name_rules=str(fixture / "historical-name-rules.json"),
                name_policy=str(fixture / "name-policy.json"),
                reviewed_name_contexts=str(fixture / "reviewed-name-contexts.json"),
                **({} if historical else {"asset_root": "assets"}),
            )
        )
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=[*WEB_VARIANTS, "auth"])
    variant = parser.parse_args().variant
    historical = variant == "historical"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inputs = materialize(root, historical)
        command = [sys.executable]
        if variant == "auth":
            command.extend(["scripts/check_auth_browser.py", "--workshop-inputs", str(inputs)])
        else:
            kind = "historical" if historical else "current"
            command.extend(
                [
                    "scripts/check_web.py",
                    *WEB_VARIANTS[variant],
                    "--inputs",
                    str(inputs),
                    "--expectations",
                    str(SCENARIOS / (kind + ".json")),
                ]
            )
            if historical:
                command.extend(["--historical-root", str(root)])
            if variant == "source-preview":
                command.extend(["--source-overlay", str(root / "sources")])
            if variant.startswith("names-"):
                command.extend(["--names-overlay", str(root / "names")])
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
