import os
import subprocess
import time

import pytest

vm_integration = pytest.mark.skipif(
    not (os.environ.get("EWE_LIVE_TESTS") == "1" and os.environ.get("SSHPASS")),
    reason="Requires EWE_LIVE_TESTS=1 and SSHPASS for VM lifecycle",
)

VM_USER = os.environ.get("EWE_VM_USER", "dexter")
VM_HOST = os.environ.get("EWE_VM_HOST", "192.168.64.2")
SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "PreferredAuthentications=password",
    "-o",
    "PubkeyAuthentication=no",
]
PLUGIN_LOG = r"%APPDATA%\Ecopath with Ecosim\EwE_MCP_Plugin.log"
DIALOG_SWEEPER_LOG = r"%APPDATA%\Ecopath with Ecosim\EwE_MCP_DialogSweeper.log"

_SSH_RETRY_ATTEMPTS = 3
_SSH_RETRY_DELAY = 5


def _env():
    return {**os.environ, "SSHPASS": os.environ["SSHPASS"]}


def _run_with_ssh_retry(cmd, **kwargs):
    for attempt in range(_SSH_RETRY_ATTEMPTS):
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=_env(), **kwargs
        )
        if result.returncode == 0:
            return result
        if "Permission denied" not in result.stderr:
            return result
        if attempt < _SSH_RETRY_ATTEMPTS - 1:
            time.sleep(_SSH_RETRY_DELAY)
    return result


@vm_integration
def test_build_plugin_on_vm():
    result = _run_with_ssh_retry(["./scripts/build-plugin-vm.sh"], timeout=120)
    assert result.returncode == 0, f"build failed:\n{result.stderr}"


@vm_integration
def test_deploy_plugin_to_vm():
    result = _run_with_ssh_retry(["./scripts/deploy-built-plugin-vm.sh"], timeout=120)
    assert result.returncode == 0, f"deploy failed:\n{result.stderr}"


@vm_integration
def test_launch_ewe_on_vm():
    result = _run_with_ssh_retry(["./scripts/launch-ewe-vm.sh"], timeout=180)
    assert result.returncode == 0, f"launch failed:\n{result.stderr}"


@vm_integration
def test_smoke_after_launch():
    result = _run_with_ssh_retry(["./scripts/vm-live-smoke.sh"], timeout=120)
    assert result.returncode == 0, f"smoke failed:\n{result.stderr}"


@vm_integration
def test_collect_plugin_log():
    result = _run_with_ssh_retry(
        [
            "sshpass",
            "-e",
            "ssh",
            *SSH_OPTS,
            f"{VM_USER}@{VM_HOST}",
            f'cmd /c type "{PLUGIN_LOG}"',
        ],
        timeout=30,
    )
    assert result.returncode == 0, f"could not read plugin log:\n{result.stderr}"
    assert "Socket server started" in result.stdout, (
        f"plugin log missing 'Socket server started':\n{result.stdout}"
    )


@vm_integration
def test_collect_dialog_sweeper_log():
    result = _run_with_ssh_retry(
        [
            "sshpass",
            "-e",
            "ssh",
            *SSH_OPTS,
            f"{VM_USER}@{VM_HOST}",
            f'cmd /c type "{DIALOG_SWEEPER_LOG}"',
        ],
        timeout=30,
    )
    if result.returncode != 0 and "cannot find" in result.stderr.lower():
        pytest.skip(
            "Dialog sweeper log not present (sweeper may not have been started)"
        )
    assert result.returncode == 0, (
        f"could not read dialog sweeper log:\n{result.stderr}"
    )
