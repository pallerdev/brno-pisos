from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps():
    workflow = yaml.safe_load((ROOT / ".github/workflows/run.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["run"], workflow["jobs"]["run"]["steps"]


def test_bot_step_is_cut_before_the_job_so_state_can_still_be_saved():
    job, steps = _steps()
    bot = next(step for step in steps if step.get("run") == "python -m pisobot")
    assert bot["timeout-minutes"] < job["timeout-minutes"]


def test_state_is_committed_after_a_failed_step_but_only_json_files():
    _, steps = _steps()
    save = next(step for step in steps if step.get("name") == "Guardar estado")
    assert save["if"] == "${{ !cancelled() }}"
    adds = [line.strip() for line in save["run"].splitlines() if line.strip().startswith("git add")]
    assert adds == ["git add -- data/*.json"]


def test_interrupted_writes_are_ignored_by_git():
    assert "data/*.tmp" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
