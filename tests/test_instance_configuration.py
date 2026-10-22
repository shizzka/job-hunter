"""An unconfigured instance cannot grant worker permissions."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import config
import profile_access
import runtime_control


@pytest.mark.parametrize('kind', [None, '', 'unknown'])
def test_missing_or_invalid_instance_denies_profile_access_and_worker_launch(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    if kind is None:
        monkeypatch.delenv('JOB_HUNTER_INSTANCE_KIND', raising=False)
    else:
        monkeypatch.setenv('JOB_HUNTER_INSTANCE_KIND', kind)
    for name in ['default', 'qa', 'qa-dev']:
        assert not profile_access.profile_allowed(name)
        with pytest.raises(PermissionError):
            profile_access.require_profile(name)
    create = AsyncMock()
    popen = Mock()
    monkeypatch.setattr(runtime_control.asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(runtime_control.subprocess, 'Popen', popen)
    with pytest.raises(PermissionError):
        asyncio.run(runtime_control.run_command_capture(['synthetic-worker']))
    with pytest.raises(PermissionError):
        runtime_control.start_background_process(['synthetic-worker'],
            pid_file=str(tmp_path / 'worker.pid'), log_file=str(tmp_path / 'worker.log'))
    create.assert_not_awaited()
    popen.assert_not_called()
