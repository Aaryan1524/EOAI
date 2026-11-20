import platform
import subprocess

import pytest

from experiments.eoai_eval.isolation import ROLES, _copy_output, role_area, role_command, verify_roles


def test_each_native_role_is_denied_private_files_credentials_and_direct_network(tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("Native macOS sandbox unavailable")
    with role_area() as area:
        probe = subprocess.run(
            role_command("reviewer", area, "pass"), cwd=area, env={}, capture_output=True,
        )
    if probe.returncode == 71 and b"Operation not permitted" in probe.stderr:
        pytest.skip("Cannot install a nested sandbox inside this test host")
    assert probe.returncode == 0, probe.stderr
    private = tmp_path / "vault"
    private.mkdir()
    answer = private / "answers.json"
    answer.write_text("private answer canary")
    credential = tmp_path / "credential"
    credential.write_text("credential canary")
    evidence = verify_roles(private, answer, credential)
    assert set(evidence) == set(ROLES)
    assert all(all(checks.values()) for checks in evidence.values())
    assert not (private / ".role-write-probe").exists()


def test_output_collection_rejects_symlinked_parent_and_hardlink(tmp_path):
    private = tmp_path / "vault"
    private.mkdir()
    answer = private / "answers"
    answer.write_text("private")
    private_run = private / "plausible-run"
    private_run.mkdir()
    (tmp_path / "traces").symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="isolated output"):
        _copy_output(tmp_path / "traces" / private_run.name, tmp_path / "collected")
    public = tmp_path / "public"
    public.mkdir()
    (public / "report").hardlink_to(answer)
    with pytest.raises(ValueError, match="isolated artifact"):
        _copy_output(public, tmp_path / "collected")
