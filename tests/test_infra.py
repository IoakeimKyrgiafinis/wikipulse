"""infra/ (spec 08c): the Terraform stacks and scripts/infra.py, without running Terraform
against anything. CI also runs `terraform fmt -check` and `terraform validate`."""

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from wikipulse.config import Settings

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
SCRIPT = ROOT / "scripts" / "infra.py"
spec = importlib.util.spec_from_file_location("infra", SCRIPT)
assert spec is not None and spec.loader is not None
infra = importlib.util.module_from_spec(spec)
sys.modules["infra"] = infra
spec.loader.exec_module(infra)

VARIABLE = re.compile(r'variable "(\w+)" \{(.*?)\n\}', re.DOTALL)


def declared_variables(stack: str) -> dict[str, str]:
    """Each variable of a stack with the body of its block."""
    text = (INFRA / stack / "variables.tf").read_text(encoding="utf-8")
    return dict(VARIABLE.findall(text))


def settings() -> Settings:
    return Settings(
        _env_file=None,
        postgres_dsn="postgresql://wp:pg-secret@localhost:5432/wikipulse",
        neo4j_password=SecretStr("neo-secret"),
        s3_endpoint="http://localhost:9000",
        s3_access_key=SecretStr("access"),
        s3_secret_key=SecretStr("s3-secret"),
        user_agent="WikiPulse/0.1 (https://github.com/x/wikipulse; me@real.org)",
    )


def test_s3_server_drops_the_scheme() -> None:
    assert infra.s3_server("http://localhost:9000") == "localhost:9000"
    assert infra.s3_server("https://s3.eu-west-1.amazonaws.com") == "s3.eu-west-1.amazonaws.com"


@pytest.mark.parametrize("stack", infra.STACKS)
def test_the_script_sets_only_variables_the_stack_declares(stack: str) -> None:
    variables = infra.stack_variables(stack, settings())
    assert set(variables) <= set(declared_variables(stack))


def test_the_cluster_secret_uses_the_in_cluster_database_host() -> None:
    variables = infra.stack_variables("cluster", settings())
    assert variables["postgres_dsn"] == "postgresql://wp:pg-secret@postgres:5432/wikipulse"
    assert variables["neo4j_password"] == "neo-secret"
    assert variables["s3_secret_key"] == "s3-secret"


def test_variables_go_to_the_environment_and_never_override_yours() -> None:
    env = infra.terraform_env(
        {"s3_secret_key": "from-settings", "s3_access_key": "a"},
        {"TF_VAR_s3_secret_key": "mine", "PATH": "/bin"},
    )
    assert env["TF_VAR_s3_secret_key"] == "mine"
    assert env["TF_VAR_s3_access_key"] == "a"
    assert env["PATH"] == "/bin"


def test_terraform_runs_in_the_stack_folder() -> None:
    command = infra.terraform_command("cluster", ["plan"])
    assert command == ["terraform", f"-chdir={INFRA / 'cluster'}", "plan"]


@pytest.mark.parametrize("stack", infra.STACKS)
def test_secret_variables_have_no_default(stack: str) -> None:
    secrets = {
        name: body for name, body in declared_variables(stack).items() if "sensitive" in body
    }
    assert secrets, "every stack has secrets"
    for name, body in secrets.items():
        has_default = re.search(r"^\s*default\s*=", body, re.MULTILINE)
        assert not has_default, f"{stack}: secret variable {name} has a default"


def test_no_tf_file_assigns_a_secret_value() -> None:
    """Secrets are referenced as var.<name>, never written as literals."""
    for path in INFRA.rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"(password|secret_key|access_key|_dsn|user)\s*=\s*\"", line)
            assert not match, f"{path.name}: literal secret in {line.strip()!r}"


def test_state_and_secrets_are_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".terraform/", "*.tfstate", "*.tfstate.*", "*.tfvars"):
        assert pattern in ignored


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is not installed")
def test_terraform_files_are_formatted() -> None:
    result = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", str(INFRA)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"terraform fmt would change: {result.stdout}"
