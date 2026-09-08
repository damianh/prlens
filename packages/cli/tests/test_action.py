"""Static checks for the composite review action."""

from pathlib import Path

import yaml


ACTION = Path(__file__).parents[3] / ".github" / "actions" / "review" / "action.yml"


def _action_text() -> str:
    return ACTION.read_text()


def test_action_metadata_is_valid_yaml():
    parsed = yaml.safe_load(_action_text())
    assert parsed["runs"]["using"] == "composite"


def test_copilot_installs_fork_packages_from_action_revision():
    content = _action_text()
    assert "github.action_path" in content
    assert '"$action_root/packages/core[copilot]"' in content
    assert '"$action_root/packages/store"' in content
    assert '"$action_root/packages/cli"' in content
    assert "python -m copilot download-runtime" in content


def test_existing_providers_keep_versioned_pypi_install():
    content = _action_text()
    assert "if: inputs.model != 'copilot'" in content
    assert 'pip install "prlens[${{ inputs.model }}]==${{ inputs.version }}"' in content


def test_copilot_rejects_forks_and_untrusted_checkout():
    content = _action_text()
    assert "PRLENS_HEAD_REPOSITORY" in content
    assert "PRLENS_BASE_SHA" in content
    assert "git rev-parse HEAD" in content
    assert "pull request base SHA" in content
