import io
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from packaging.version import Version
from rich.console import Console

from redbot._update import common, internal, runner, updater
from redbot._update.__main__ import cli
from redbot.core.utils._internal_utils import AvailableVersion


@pytest.mark.parametrize("args, interactive", [([], True), (["--no-prompt"], False)])
def test_updater_prompt_mode(args, interactive):
    with cli.make_context("redbot-update", args) as context:
        assert context.params["interactive"] is interactive


@pytest.fixture
def venv(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("old interpreter")
    (venv / "pyvenv.cfg").write_text("old configuration")
    wrapper = venv / "wrapper"
    wrapper.write_text("running wrapper")
    monkeypatch.setenv(common.RUNNER_WRAPPER_EXE_ENV_VAR, str(wrapper))
    monkeypatch.setattr(common, "get_console", lambda: Console(file=io.StringIO()))
    return venv


def assert_restored(venv):
    assert {p.name for p in venv.iterdir()} == {"bin", "pyvenv.cfg", "wrapper"}
    assert (venv / "bin" / "python").read_text() == "old interpreter"
    assert (venv / "pyvenv.cfg").read_text() == "old configuration"
    assert (venv / "wrapper").read_text() == "running wrapper"


@pytest.fixture
def update(venv, monkeypatch):
    monkeypatch.setattr(
        updater, "sys", SimpleNamespace(prefix=str(venv), executable=str(venv / "bin" / "python"))
    )
    monkeypatch.setattr(updater.tempfile, "tempdir", str(venv.parent))
    monkeypatch.setattr(updater, "get_installed_extras", lambda: [])
    monkeypatch.setattr(common, "check_update_interpreter", Mock())
    update = updater.Updater(Mock())
    update.metadata = SimpleNamespace(
        latest=AvailableVersion(Version("3.5.25"), {}, commit="a" * 40, requires_python=">=3.11"),
        interpreter_exe="base-python",
        interpreter_info=Mock(sysconfig_path=Mock(return_value=str(venv / "bin"))),
        to_json_dict=lambda: {"test": True},
    )
    return update


@pytest.mark.parametrize("stage", ["metadata", "partial_move", "relocated_interpreter", "handoff"])
async def test_failed_backup_restores_environment_and_allows_retry(
    venv, update, monkeypatch, stage
):
    failure = OSError("simulated IO failure")
    backup = venv / common.OLD_VENV_BACKUP_DIR_NAME
    with monkeypatch.context() as patch:
        if stage == "metadata":
            patch.setattr(update.metadata, "to_json_dict", Mock(side_effect=failure))
        elif stage == "partial_move":
            rename = Path.rename

            def fail_second_move(path, target):
                if target.parent == backup and any(backup.iterdir()):
                    raise failure
                return rename(path, target)

            patch.setattr(Path, "rename", fail_second_move)
        elif stage == "relocated_interpreter":
            patch.setattr(common, "check_update_interpreter", Mock(side_effect=failure))
        else:
            patch.setattr(runner, "make_exec_request", Mock(side_effect=failure))
        with pytest.raises(OSError, match="simulated IO failure"):
            await update._update_with_fresh_venv()
    assert_restored(venv)
    assert not list(venv.parent.glob("redbot-update-metadata-*"))

    request = Mock(side_effect=SystemExit(3))
    monkeypatch.setattr(runner, "make_exec_request", request)
    with pytest.raises(SystemExit) as moved:
        await update._update_with_fresh_venv()
    assert moved.value.code == 3
    assert (backup / "bin" / "python").read_text() == "old interpreter"
    metadata_vars = request.call_args.kwargs["set_env_vars"]
    metadata_file = Path(metadata_vars[common.INTERNAL_UPDATER_METADATA_ENV_VAR])
    assert metadata_file.is_file()


async def test_existing_backup_is_preserved(venv, update):
    backup = venv / common.OLD_VENV_BACKUP_DIR_NAME
    backup.mkdir()
    (backup / "leftover").write_text("previous installation")
    with pytest.raises(SystemExit) as refused:
        await update._update_with_fresh_venv()
    assert refused.value.code == 1
    assert (backup / "leftover").read_text() == "previous installation"
    assert (venv / "bin" / "python").is_file()


@pytest.mark.parametrize(
    "failed_stage",
    ["venv", "pip_upgrade", "pip_install", "pip_check", "import", "handoff", "interrupt"],
)
def test_failed_reinstall_restores_environment(venv, monkeypatch, failed_stage):
    backup = venv / common.OLD_VENV_BACKUP_DIR_NAME
    backup.mkdir()
    for name in ("bin", "pyvenv.cfg"):
        (venv / name).rename(backup / name)
    metadata = venv.parent / "metadata.json"
    metadata.write_text("{}")
    monkeypatch.setenv(common.INTERNAL_UPDATER_METADATA_ENV_VAR, str(metadata))
    monkeypatch.setattr(
        runner, "get_request_output", lambda: runner.ExecRequestOutput(runner.RequestType.exec)
    )
    stages = iter(["venv", "pip_upgrade", "pip_install", "pip_check", "import"])
    attempted = []

    def run(command):
        stage = next(stages)
        attempted.append(stage)
        if stage == "venv":
            (venv / "bin").mkdir()
            (venv / "bin" / "python").write_text("partial installation")
            (venv / "pyvenv.cfg").write_text("new configuration")
            (venv / "new-file").touch()
        if stage == failed_stage:
            if stage == "venv":
                raise OSError("missing interpreter")
            raise subprocess.CalledProcessError(1, command)
        if failed_stage == "interrupt" and stage == "pip_install":
            raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "check_call", run)
    request = Mock(side_effect=OSError("write failed"))
    monkeypatch.setattr(runner, "make_exec_request", request)
    with pytest.raises(SystemExit) as failed:
        internal.reinstall.callback("base-python", venv, venv / "bin", "Red-DiscordBot")
    assert failed.value.code == 1
    if failed_stage == "handoff":
        request.assert_called_once()
    else:
        request.assert_not_called()
        assert attempted[-1] == ("pip_install" if failed_stage == "interrupt" else failed_stage)
    assert_restored(venv)
    assert not metadata.exists()


def test_verified_reinstall_keeps_backup_for_finish_stage(venv, monkeypatch):
    backup = venv / common.OLD_VENV_BACKUP_DIR_NAME
    backup.mkdir()
    (venv / "bin").rename(backup / "bin")
    monkeypatch.setattr(
        runner, "get_request_output", lambda: runner.ExecRequestOutput(runner.RequestType.exec)
    )
    install = Mock()
    request = Mock(side_effect=SystemExit(3))
    monkeypatch.setattr(subprocess, "check_call", install)
    monkeypatch.setattr(runner, "make_exec_request", request)
    with pytest.raises(SystemExit) as handoff:
        internal.reinstall.callback("base-python", venv, venv / "bin", "Red-DiscordBot")
    assert handoff.value.code == 3
    executable, subcommand = request.call_args.args
    assert subcommand == "finish-update"
    assert [call.args[0] for call in install.call_args_list] == [
        ("base-python", "-m", "venv", str(venv)),
        (executable, "-m", "pip", "install", "-U", "pip"),
        (executable, "-m", "pip", "install", "Red-DiscordBot"),
        (executable, "-m", "pip", "check"),
        (executable, "-I", "-c", "from redbot._update import internal"),
    ]
    assert (backup / "bin" / "python").read_text() == "old interpreter"


def test_partial_venv_removal_unlinks_directory_symlinks(venv, tmp_path):
    (venv / common.OLD_VENV_BACKUP_DIR_NAME).mkdir()
    external_lib = tmp_path / "library"
    external_lib.mkdir()
    (external_lib / "keep").touch()
    try:
        (venv / "lib64").symlink_to(external_lib, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"this system cannot create a directory symlink: {error}")
    internal._remove_new_venv(venv)
    assert not (venv / "lib64").is_symlink()
    assert (external_lib / "keep").is_file()
    assert (venv / "wrapper").is_file()
