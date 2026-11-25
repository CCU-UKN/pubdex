"""One version identifies the application images and the chart."""
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = ("VERSION", "compose.yaml", "charts/pubdex/Chart.yaml", "charts/pubdex/values.yaml")


def assert_one_version(files) -> None:
    version = files["VERSION"].strip()
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    chart = yaml.safe_load(files["charts/pubdex/Chart.yaml"])
    assert chart["version"] == chart["appVersion"] == version
    for variable, repository in (("PEOPLE_PUBS_IMAGE", "pubdex"), ("PEOPLE_PUBS_POSTGRES_IMAGE", "pubdex-postgres")):
        defaults = set(re.findall(r"\$\{" + variable + r":-([^}]+)\}", files["compose.yaml"]))
        assert defaults == {f"ghcr.io/ccu-ukn/{repository}:{version}"}
    values = yaml.safe_load(files["charts/pubdex/values.yaml"])
    assert values["image"]["tag"] == values["postgresImage"]["tag"] == version


def test_application_compose_and_chart_share_one_version() -> None:
    assert_one_version({name: (ROOT / name).read_text(encoding="utf-8") for name in FILES})


@pytest.mark.parametrize("name, occurrence", [("VERSION", 0), ("compose.yaml", 0), ("compose.yaml", 1),
    ("charts/pubdex/Chart.yaml", 0), ("charts/pubdex/Chart.yaml", 1),
    ("charts/pubdex/values.yaml", 0), ("charts/pubdex/values.yaml", 1)])
def test_a_version_disagreement_fails(name, occurrence) -> None:
    files = {path: (ROOT / path).read_text(encoding="utf-8") for path in FILES}
    version = files["VERSION"].strip()
    if name == "compose.yaml":
        pattern = r"(?<=:-ghcr.io/ccu-ukn/pubdex:)" + re.escape(version) + r"(?=\})"
        if occurrence:
            pattern = pattern.replace("pubdex:", "pubdex-postgres:")
    elif name.endswith("Chart.yaml"):
        pattern = (r"(?m)^version: " if not occurrence else r"(?m)^appVersion: ") + r'"?' + re.escape(version) + r'"?'
    elif name.endswith("values.yaml"):
        pattern = r'(?m)^  tag: "' + re.escape(version) + r'"'
    else:
        pattern = re.escape(version)
    matches = list(re.finditer(pattern, files[name]))
    match = matches[occurrence if name.endswith("values.yaml") else 0]
    files[name] = files[name][:match.start()] + match.group().replace(version, "99.99.99") + files[name][match.end():]
    with pytest.raises(AssertionError):
        assert_one_version(files)
