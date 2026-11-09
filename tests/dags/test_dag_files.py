"""Checks of the DAG files that need no Airflow (they run in the normal test suite).

The DAG files are parsed as Python syntax trees, not imported: Airflow isn't installed
in the project's environment. `test_dag_import.py` imports them for real, inside the
Airflow image (see the `dags` job in .github/workflows/ci.yml).
"""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
DAG_FILES = sorted(DAGS.glob("*.py"))
REQUIREMENTS = ROOT / "docker" / "airflow" / "requirements-jobs.txt"
# What a DAG file may import: Airflow, the standard library, and the shared helper.
ALLOWED_TOP_LEVEL = {"airflow", "wikipulse_jobs"}


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_both_dags_exist() -> None:
    assert {path.stem for path in DAG_FILES} >= {"daily_eval", "daily_stats"}


@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_file_compiles(path: Path) -> None:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_files_hold_no_business_logic(path: Path) -> None:
    """Requirement 2: tasks call wikipulse modules; DAG files never import them."""
    outside = {
        name
        for name in imports(path)
        if name not in ALLOWED_TOP_LEVEL and name not in sys.stdlib_module_names
    }
    assert not outside, f"{path.name} imports {outside}"


@pytest.mark.parametrize("path", DAG_FILES, ids=lambda p: p.name)
def test_dag_tasks_run_existing_job_modules(path: Path) -> None:
    """Every `job_task(..., "<module>", "<step>")` names a real wikipulse.jobs command."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "job_task":
            module, step = (arg.value for arg in node.args[1:3])
            source = ROOT / "wikipulse" / "jobs" / f"{module}.py"
            assert source.exists(), f"no wikipulse/jobs/{module}.py"
            assert f'"{step}"' in source.read_text(encoding="utf-8"), f"{module} has no {step}"


def test_image_requirements_match_the_lockfile() -> None:
    """The jobs' environment in the Airflow image uses the versions of uv.lock."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    pins = [
        line.split("==")
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert pins
    for name, version in pins:
        assert locked[name.split("[")[0]] == version, f"{name}: {version} vs uv.lock"
