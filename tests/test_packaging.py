import subprocess
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULESETS_DIR = PROJECT_ROOT / "src" / "jevapan" / "rulesets"


def test_wheel_contains_each_ruleset_exactly_once(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(PROJECT_ROOT)],
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    expected = sorted(f"jevapan/rulesets/{p.name}" for p in RULESETS_DIR.glob("*.yaml"))
    assert expected
    assert sorted(n for n in names if n.startswith("jevapan/rulesets/")) == expected
