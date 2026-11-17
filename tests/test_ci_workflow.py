"""CI keeps the committed admin build honest: it is rebuilt and compared, and the browser gates run."""
import json
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def admin_ui_job():
    return YAML(typ="safe").load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))["jobs"]["admin-ui"]


def test_admin_ui_job_rebuilds_dist_checks_drift_and_runs_the_browser_gates_in_order():
    job = admin_ui_job()
    assert job["runs-on"] == "windows-latest" and job["defaults"]["run"]["working-directory"] == "web"
    runs = [step.get("run", "") for step in job["steps"]]
    wanted = ["npm ci", "npm run build", "git status --porcelain -- dist", "playwright install chromium",
              "playwright test", "npm run test:design"]
    positions = [next((index for index, run in enumerate(runs) if needle in run), None) for needle in wanted]
    assert None not in positions, dict(zip(wanted, positions))
    assert positions == sorted(set(positions)), "steps must run in this order"
    drift = runs[positions[2]]
    assert "exit 1" in drift  # any change under dist (modified, deleted or new hashed assets) fails the job


def test_ci_runs_every_unit_test_file_of_the_admin_ui():
    # the step names its files one by one (a glob is not expanded by node on Windows), so a new file has to be added to it
    workflow = YAML(typ="safe").load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for step in workflow["jobs"]["tests"]["steps"]]
    listed = {word for run in runs if "node --test" in run for word in run.split() if word.startswith("web/tests/")}
    on_disk = {path.relative_to(ROOT).as_posix() for path in (ROOT / "web" / "tests").glob("*.test.mjs")}
    assert on_disk and listed == on_disk


def test_design_gate_is_an_npm_script_and_the_docs_install_from_the_lockfile():
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test:design"] == "node scripts/accuracy_report.mjs"
    assert (ROOT / "web" / "scripts" / "accuracy_report.mjs").is_file()
    assert (ROOT / "web" / "package-lock.json").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    build_lines = [line for line in readme.splitlines() if "cd web; " in line and "npm run build" in line]
    assert build_lines and all("npm ci" in line and "npm install" not in line for line in build_lines)
