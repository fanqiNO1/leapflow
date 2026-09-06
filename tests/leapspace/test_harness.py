"""Hermetic unit tests for LeapAppHarness (fake actor, no sandbox).

"""

import asyncio
import json
import logging
import platform
from pathlib import Path

import pytest

import leapspace.app_space.harness as harness_module
from leapspace.app_space.config import AppTaskConfig
from leapspace.app_space.harness import LeapAppHarness
from leapspace.app_space.utils import LeapAppImage, get_image_python

PYTHON = get_image_python("linux")

CONFIG = """\
id: task-t
title: wire me
app_ids: [chat, notes]
instruction: x
hooks:
  chat:
    before_launch: seed
    after_message_sent: boss_followup
"""

ACTION = "def seed(app):\n    app.reset({})\n"


class FakeActor:
    """Records the fs calls _prepare_files makes through the actor."""

    def __init__(self):
        self.calls = []

    async def fs_mkdir(self, path):
        self.calls.append(("mkdir", path))

    async def fs_write(self, path, content):
        self.calls.append(("write", path, content))


def prepare(tmp_path):
    """Run _prepare_files against a real config; return the actor's calls."""
    (tmp_path / "action.py").write_text(ACTION)
    (tmp_path / "config.yaml").write_text(CONFIG)
    config = AppTaskConfig.load(tmp_path / "config.yaml")
    actor = FakeActor()
    harness = LeapAppHarness(LeapAppImage.LINUX)
    asyncio.run(harness._prepare_files(config, actor))
    return actor.calls


def writes(calls):
    return {call[1]: call[2] for call in calls if call[0] == "write"}


def test_writes_hooks_pair_per_app(tmp_path):
    calls = prepare(tmp_path)
    for app_id in ("chat", "notes"):
        state_dir = f"/tmp/leapspace/{app_id}"
        hooks_json_at = next(
            i
            for i, call in enumerate(calls)
            if call[0] == "write" and call[1] == f"{state_dir}/hooks.json"
        )
        # hooks must land before launch, so the dir is created first
        assert calls.index(("mkdir", state_dir)) < hooks_json_at
    assert sorted(writes(calls)) == [
        "/tmp/leapspace/chat/hooks.json",
        "/tmp/leapspace/chat/hooks.py",
        "/tmp/leapspace/notes/hooks.json",
        "/tmp/leapspace/notes/hooks.py",
    ]


def test_hooks_json_carries_only_the_apps_mapping(tmp_path):
    assert json.loads(writes(prepare(tmp_path))["/tmp/leapspace/chat/hooks.json"]) == {
        "before_launch": "seed",
        "after_message_sent": "boss_followup",
    }
    assert json.loads(writes(prepare(tmp_path))["/tmp/leapspace/notes/hooks.json"]) == {}


def test_hooks_py_is_action_copy(tmp_path):
    for path, content in writes(prepare(tmp_path)).items():
        if path.endswith("hooks.py"):
            assert content == ACTION


def test_resolves_state_root_from_image_not_host_os(tmp_path, monkeypatch):
    # The harness resolves the sandbox's OS from its image preset; host-side
    # detection must never leak into sandbox-destined paths.
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert all("/tmp/leapspace/" in call[1] for call in prepare(tmp_path))


class LaunchActor:
    """Fakes the actor surface the launch path drives.

    state.json is deemed written once `launches_required` shells have
    fired, which doubles as the concurrency canary: a launcher that
    starts apps one at a time never sees the first app's file.
    """

    def __init__(self, envelope, launches_required=1):
        self.envelope = envelope
        self.launches_required = launches_required
        self.launched = 0
        self.calls = []

    async def shell_checked(self, command, timeout=30, background=False):
        self.calls.append(("shell", command, background))
        self.launched += 1
        return None

    async def fs_exists(self, path):
        self.calls.append(("exists", path))
        return self.launched >= self.launches_required

    async def fs_read(self, path):
        self.calls.append(("read", path))
        return json.dumps(self.envelope)

    async def wait_for_window(self, app_title):
        self.calls.append(("wait", app_title))
        return {}


def launch_config(app_ids, interface=None):
    return AppTaskConfig(
        id="task-t",
        title="t",
        app_ids=app_ids,
        instruction="x",
        action_path=Path("action.py"),
        interface=interface or {},
    )


def launch(actor, config):
    asyncio.run(LeapAppHarness(LeapAppImage.LINUX)._launch_apps(config, actor))


def test_launch_waits_on_envelope_app_title():
    actor = LaunchActor({"app_id": "chat", "app_title": "LeapChat", "interface": []})
    launch(actor, launch_config(("chat",)))
    assert actor.calls == [
        ("shell", f"{PYTHON} -m leapspace.app_space.apps.chat", True),
        ("exists", "/tmp/leapspace/chat/state.json"),
        ("read", "/tmp/leapspace/chat/state.json"),
        ("wait", "LeapChat"),
    ]


def test_launch_times_out_when_state_json_never_appears(monkeypatch):
    monkeypatch.setattr(harness_module, "LAUNCH_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(harness_module, "LAUNCH_READY_POLL_S", 0.01)
    # launches_required=2 with a single-app task: state.json never appears
    actor = LaunchActor({"app_id": "chat", "app_title": "LeapChat", "interface": []}, 2)
    with pytest.raises(RuntimeError, match="never wrote /tmp/leapspace/chat/state.json"):
        launch(actor, launch_config(("chat",)))


def test_launch_rejects_unknown_app_id():
    actor = LaunchActor({})
    harness = LeapAppHarness(LeapAppImage.LINUX)
    with pytest.raises(ValueError, match="unknown app_id 'ghost'"):
        asyncio.run(harness._launch_app("ghost", launch_config(("ghost",)), actor))


def test_launch_reports_all_failing_apps(monkeypatch, caplog):
    monkeypatch.setattr(harness_module, "LAUNCH_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(harness_module, "LAUNCH_READY_POLL_S", 0.01)
    monkeypatch.setattr(
        harness_module,
        "APP_MODULES",
        {"chat": "leapspace.app_space.apps.chat", "notes": "leapspace.app_space.apps.notes"},
    )
    # state.json never appears (launches_required is unreachable for two
    # apps), so both apps fail their readiness poll
    actor = LaunchActor({"app_title": "Any", "interface": []}, launches_required=3)
    with caplog.at_level(logging.ERROR, logger="leapspace.app_space.harness"):
        with pytest.raises(RuntimeError, match="never wrote /tmp/leapspace/chat/state.json"):
            launch(actor, launch_config(("chat", "notes")))
    # the first failure (config order) is raised; the sibling is logged, not lost
    assert any("notes" in record.getMessage() for record in caplog.records)


def test_launch_runs_apps_concurrently(monkeypatch):
    # state.json counts as written only once BOTH apps' shells fired, so a
    # serial launcher would poll the first app into the (short) timeout.
    monkeypatch.setattr(harness_module, "LAUNCH_READY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(harness_module, "LAUNCH_READY_POLL_S", 0.01)
    monkeypatch.setattr(
        harness_module,
        "APP_MODULES",
        {"chat": "leapspace.app_space.apps.chat", "notes": "leapspace.app_space.apps.notes"},
    )
    actor = LaunchActor({"app_title": "Any", "interface": []}, launches_required=2)
    launch(actor, launch_config(("chat", "notes")))
    assert [call[1] for call in actor.calls if call[0] == "shell"] == [
        f"{PYTHON} -m leapspace.app_space.apps.chat",
        f"{PYTHON} -m leapspace.app_space.apps.notes",
    ]
    assert all(call == ("wait", "Any") for call in actor.calls if call[0] == "wait")


def test_interface_precheck_requires_persisted_names():
    envelope = {"app_id": "chat", "app_title": "LeapChat", "interface": ["reset"]}
    launch(LaunchActor(envelope), launch_config(("chat",), interface={"chat": ("reset",)}))

    actor = LaunchActor(envelope)
    with pytest.raises(RuntimeError, match="does not expose"):
        launch(actor, launch_config(("chat",), interface={"chat": ("send_message",)}))
