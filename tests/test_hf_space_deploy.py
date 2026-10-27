"""Offline tests for deploy/hf-space/deploy.sh and the `deploy-space` CI job.

deploy.sh is run for real against a local bare git repository standing in for the Space, so the
staging, placeholder substitution, replace-and-push, no-op, and token-handling logic are all
exercised without network or credentials.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPACE_DIR = REPO / "deploy" / "hf-space"
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
SHA = "a" * 40

needs_bash_git = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("git"),
    reason="needs bash and git on a POSIX shell",
)


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def fake_hub(tmp_path):
    """A bare repo at <base>/owner/space seeded like a fresh Space (README + .gitattributes)."""
    base = tmp_path / "base"
    bare = base / "owner" / "space"
    bare.mkdir(parents=True)
    _git("init", "--bare", "-q", "-b", "main", cwd=bare)
    seed = tmp_path / "seed"
    _git("clone", "-q", str(bare), str(seed), cwd=tmp_path)
    (seed / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    (seed / "README.md").write_text("placeholder space readme\n", encoding="utf-8")
    (seed / "old.txt").write_text("left over from an earlier deploy\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    return base, bare


def _deploy(base: Path, *, sha: str = SHA, space: str = "owner/space", script=None, **env):
    run_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(base.parent),
               "HF_GIT_BASE": f"file://{base}", "HF_SPACE_ID": space, "RTDP_DEPLOY_SHA": sha,
               **env}
    return subprocess.run(["bash", str(script or SPACE_DIR / "deploy.sh")], env=run_env,
                          capture_output=True, text=True)


def _head(bare: Path) -> str:
    return _git("rev-list", "--count", "main", cwd=bare)


@needs_bash_git
def test_deploy_replaces_files_fills_the_placeholder_and_keeps_gitattributes(fake_hub, tmp_path):
    base, bare = fake_hub
    result = _deploy(base)
    assert result.returncode == 0, result.stderr
    assert f"Pushed {SHA} to owner/space (main)" in result.stdout

    names = _git("ls-tree", "--name-only", "main", cwd=bare).split()
    assert sorted(names) == [".gitattributes", "README.md", "app.py", "requirements.txt"]
    requirements = _git("show", "main:requirements.txt", cwd=bare)
    assert f"@{SHA}" in requirements and "__RTDP_COMMIT_SHA__" not in requirements
    assert _git("show", "main:README.md", cwd=bare).startswith("---\n")  # the Space config
    assert _git("log", "-1", "--format=%s|%an", "main", cwd=bare) == f"Deploy {SHA}|rtdp deploy"


@needs_bash_git
def test_second_deploy_of_the_same_commit_is_a_noop(fake_hub):
    base, bare = fake_hub
    assert _deploy(base).returncode == 0
    before = _head(bare)
    again = _deploy(base)
    assert again.returncode == 0 and "nothing to push" in again.stdout
    assert _head(bare) == before


@needs_bash_git
def test_a_new_commit_adds_exactly_one_commit_without_force(fake_hub):
    base, bare = fake_hub
    _deploy(base)
    before = _head(bare)
    assert _deploy(base, sha="b" * 40).returncode == 0
    assert int(_head(bare)) == int(before) + 1
    assert "Deploy " + "b" * 40 == _git("log", "-1", "--format=%s", "main", cwd=bare)


@needs_bash_git
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"space": "no-slash"}, "owner/name"), ({"space": "a/b/c"}, "owner/name"),
     ({"space": "owner/sp ace"}, "owner/name"), ({"sha": "abc"}, "40-character")],
)
def test_bad_inputs_are_rejected_before_anything_is_cloned(fake_hub, kwargs, message):
    base, bare = fake_hub
    before = _head(bare)
    result = _deploy(base, **kwargs)
    assert result.returncode == 2 and message in result.stderr
    assert _head(bare) == before


@needs_bash_git
def test_missing_placeholder_fails_the_deploy(fake_hub, tmp_path):
    base, bare = fake_hub
    broken = tmp_path / "broken"
    shutil.copytree(SPACE_DIR, broken)
    (broken / "requirements.txt").write_text("pyiceberg==0.11.1\nrtdp\n", encoding="utf-8")
    before = _head(bare)
    result = _deploy(base, script=broken / "deploy.sh")
    assert result.returncode == 1 and "must pin rtdp" in result.stderr
    assert _head(bare) == before


@needs_bash_git
def test_token_never_appears_in_output_or_the_pushed_files(fake_hub):
    base, bare = fake_hub
    token = "hf_" + "S3cretToken" * 3
    result = _deploy(base, HF_TOKEN=token)  # file:// remote ignores the header; logic still runs
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr
    assert "Authorization" not in result.stdout + result.stderr
    for name in ("requirements.txt", "app.py", "README.md"):
        assert token not in _git("show", f"main:{name}", cwd=bare)


@needs_bash_git
def test_actions_masks_the_encoded_token_only_inside_github_actions(fake_hub):
    base, _ = fake_hub
    local = _deploy(base, HF_TOKEN="hf_tok")
    assert "::add-mask::" not in local.stdout
    in_ci = _deploy(base, HF_TOKEN="hf_tok", GITHUB_ACTIONS="true", sha="c" * 40)
    assert "::add-mask::" in in_ci.stdout


# ------------------------------------------------------------------ the CI job
def _job_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z][\w-]*:\n|\Z)", CI,
                      re.MULTILINE | re.DOTALL)
    assert match, f"job {name} not found"
    return match[1]


def test_ssh_deploy_is_gone_and_deploy_space_replaces_it():
    assert "DEPLOY_SSH" not in CI and "ssh-keyscan" not in CI
    assert re.search(r"^  deploy:\n", CI, re.MULTILINE) is None
    assert "publish-image:" in CI  # the image still publishes


def test_deploy_space_is_gated_and_uses_no_third_party_action():
    block = _job_block("deploy-space")
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in block
    assert "environment: production" in block
    assert "needs: [test, integration-localstack, docker-smoke]" in block
    assert re.findall(r"uses: (\S+)", block) == ["actions/checkout@v4"]
    assert "secrets.HF_TOKEN" in block and "vars.HF_SPACE_ID" in block
    assert "github.sha" in block and "deploy/hf-space/deploy.sh" in block
    assert "--force" not in block and "-f " not in block.replace("env:", "")


def test_deploy_script_has_no_force_push_and_no_shell_tracing():
    script = (SPACE_DIR / "deploy.sh").read_text(encoding="utf-8")
    assert "--force" not in script and "push -f" not in script
    assert not re.search(r"^\s*set\s+-\w*x", script, re.MULTILINE)
