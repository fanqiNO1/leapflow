"""LeapAppHarness — end-to-end orchestration of one LeapSpace task run.

Loads a task config, lints it, boots a disposable sandbox, injects the
task's wiring, and drives the apps. The harness owns no verdict: the
in-sandbox expect's PASS/FAIL lines + exit code are the only ground
truth. Host-side only; in-sandbox code never imports this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from cua_sandbox import Sandbox
from cua_sandbox.runtime import QEMURuntime

from leapspace.app_space.apps import APP_MODULES
from leapspace.app_space.actor import LeapAppActor
from leapspace.app_space.action_lint import lint_task
from leapspace.app_space.config import AppTaskConfig
from leapspace.app_space.utils import (
    LeapAppImage,
    get_image,
    get_image_python,
    get_sandbox_state_dir,
)

logger = logging.getLogger(__name__)

LAUNCH_READY_TIMEOUT_S = 60.0
LAUNCH_READY_POLL_S = 1.0


class LeapAppHarness:
    """Single-task orchestrator bound to a sandbox image preset.

    Run-invariant choices (image preset, sandbox name) live on the
    instance; the per-run input — the task config — travels through
    run_task().
    """

    def __init__(
        self,
        image: LeapAppImage,
        sandbox_name: str | None = None,
    ) -> None:
        """Bind the image preset; sandbox name defaults to a per-image name."""
        self.image = image
        self.sandbox_name = sandbox_name or f"leapspace-app-{image.value}"

    async def _prepare_files(self, config: AppTaskConfig, actor: LeapAppActor) -> None:
        """Inject the task's wiring into each app's state dir.

        Every app gets hooks.json (the config.hooks mapping, read by the
        in-app registry with stdlib json) and hooks.py, a copy of
        config.action_path — the same file doubles as the in-sandbox
        verdict program (its __main__ runs expect()), so hooks and
        verdict share one source. Writes go through the actor so they
        land inside the sandbox.
        """
        action_source = config.action_path.read_text()
        state_root = get_sandbox_state_dir(in_sandbox=False, system=self.image.value)
        for app_id in config.app_ids:
            state_dir = state_root / app_id
            await actor.fs_mkdir(str(state_dir))
            await actor.fs_write(
                str(state_dir / "hooks.json"),
                json.dumps(config.hooks.get(app_id, {})),
            )
            await actor.fs_write(str(state_dir / "hooks.py"), action_source)

    async def _launch_apps(self, config: AppTaskConfig, actor: LeapAppActor) -> None:
        """Start every configured app concurrently and wait until all settle.

        gather() overlaps the slow Qt startups. return_exceptions=True
        collects instead of racing: every failing app is reported (the
        extras logged, the first re-raised), and healthy siblings die
        with the sandbox at teardown — no cleanup needed here.
        """
        results = await asyncio.gather(
            *(self._launch_app(app_id, config, actor) for app_id in config.app_ids),
            return_exceptions=True,
        )
        failures = [
            (app_id, result)
            for app_id, result in zip(config.app_ids, results)
            if isinstance(result, BaseException)
        ]
        if failures:
            for app_id, exc in failures[1:]:
                logger.error("app %s also failed to launch", app_id, exc_info=exc)
            raise failures[0][1]

    async def _launch_app(
        self, app_id: str, config: AppTaskConfig, actor: LeapAppActor
    ) -> dict[str, Any]:
        """Start one app in the background and wait until it is usable.

        Usable means the first state.json is written (construction,
        before_launch hooks, initial persist), a window with the
        envelope's app_title is on screen, and the app's persisted
        interface covers the names config.interface declares for it.
        Returns the envelope so callers can run further checks without
        re-reading it.
        """
        app_module = APP_MODULES.get(app_id)
        if app_module is None:
            raise ValueError(
                f"unknown app_id {app_id!r}; must be one of {sorted(APP_MODULES)}"
            )
        # background=True: pid only, no exit code — startup crashes are
        # caught by the state poll below, not by this call
        python = get_image_python(system=self.image.value)
        await actor.shell_checked(f"{python} -m {app_module}", background=True)

        envelope = await self._wait_for_app_state(app_id, actor)
        await actor.wait_for_window(envelope["app_title"])

        # config declares the mutation budget; the app's persisted interface
        # is what it actually exposes — the latter must win
        missing = set(config.interface.get(app_id, ())) - set(envelope["interface"])
        if missing:
            raise RuntimeError(
                f"app {app_id!r} does not expose interface {sorted(missing)} "
                f"required by task {config.id}"
            )
        return envelope

    async def _wait_for_app_state(
        self, app_id: str, actor: LeapAppActor
    ) -> dict[str, Any]:
        """Poll the app's state.json until its first persist lands; return it.

        background launches report no exit code, so this poll doubles as
        the crash detector: an app that dies during startup shows up as
        a file that never appears.
        """
        state_path = (
            get_sandbox_state_dir(in_sandbox=False, system=self.image.value)
            / app_id
            / "state.json"
        )
        deadline = time.monotonic() + LAUNCH_READY_TIMEOUT_S
        while not await actor.fs_exists(str(state_path)):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"app {app_id!r} never wrote {state_path} within "
                    f"{LAUNCH_READY_TIMEOUT_S}s — startup crash or hung app"
                )
            await asyncio.sleep(LAUNCH_READY_POLL_S)
        # the first persist proves __init__ + before_launch hooks are done;
        # the write is atomic, so an existing file is never half-written
        return json.loads(await actor.fs_read(str(state_path)))

    async def run_task(
        self,
        config_path: str | Path,
        mode: Literal["signal", "e2e"]
    ) -> None:
        """Run one task end-to-end: load → lint → boot → drive → teardown.

        Lint problems are task-authoring bugs — fail fast before paying
        any sandbox cost. The sandbox lives for exactly this run; all
        phases run inside its context and teardown runs on exit.
        """
        config = AppTaskConfig.load(config_path)
        lint_problems = lint_task(config)
        if lint_problems:
            for problem in lint_problems:
                logger.error(problem)
            raise RuntimeError(f"task {config_path} has {len(lint_problems)} lint problems")

        async with Sandbox.ephemeral(
            image=get_image(self.image),
            name=self.sandbox_name,
            local=True,
            runtime=QEMURuntime(mode="bare-metal"),
        ) as sandbox:
            async with LeapAppActor(sandbox=sandbox) as actor:
                pass
