"""Plugin file watcher for automatic hot-reload.

Monitors the tool plugins directory for file changes and triggers reload
when a plugin module is modified. This is a development convenience feature.

Usage:
    watcher = PluginFileWatcher(plugins_dir)
    await watcher.start()   # begins watching
    # ... on file change, calls reload_plugin(plugin_id) automatically
    await watcher.stop()
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PluginFileWatcher:
    """Watches the plugins/ directory and auto-reloads on changes.

    Only active in development/debug mode (controlled by Settings).
    Uses watchdog for event-driven file monitoring.
    """

    def __init__(self, plugins_dir: Optional[Path] = None):
        self._plugins_dir = plugins_dir or self._default_plugins_dir()
        self._observer = None
        self._debounce_task: Optional[asyncio.Task] = None
        self._pending_reloads: set[str] = set()

    @staticmethod
    def _default_plugins_dir() -> Path:
        import leapflow.tools.plugins as pkg

        return Path(pkg.__file__).parent

    async def start(self) -> None:
        """Start watching the plugins directory."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog not available; plugin file watcher disabled")
            return

        class _Handler(FileSystemEventHandler):
            def __init__(self, watcher: "PluginFileWatcher"):
                super().__init__()
                self._watcher = watcher

            def on_modified(self, event) -> None:
                if event.src_path.endswith('.py') and not event.is_directory:
                    self._watcher._schedule_reload(event.src_path)

        self._observer = Observer()
        self._observer.schedule(
            _Handler(self), str(self._plugins_dir), recursive=False
        )
        self._observer.start()
        logger.info("Plugin file watcher active on %s", self._plugins_dir)

    def _schedule_reload(self, file_path: str) -> None:
        """Debounce rapid file changes (editors may write multiple times)."""
        plugin_id = Path(file_path).stem  # e.g., "text_utils.py" → "text_utils"
        if plugin_id.startswith("__"):
            return  # skip __init__.py, __pycache__ artifacts
        self._pending_reloads.add(plugin_id)
        # Schedule debounced execution
        try:
            loop = asyncio.get_running_loop()
            if self._debounce_task is None or self._debounce_task.done():
                self._debounce_task = loop.create_task(self._debounced_reload())
        except RuntimeError:
            pass  # no event loop running — skip

    async def _debounced_reload(self) -> None:
        """Wait 500ms then reload all pending plugins."""
        await asyncio.sleep(0.5)
        pending = self._pending_reloads.copy()
        self._pending_reloads.clear()
        for plugin_id in pending:
            try:
                from leapflow.tools import reload_plugin

                fiber = reload_plugin(plugin_id)
                logger.info(
                    "Auto-reloaded plugin '%s' (gen %d)",
                    plugin_id,
                    fiber.generation,
                )
            except (KeyError, RuntimeError) as exc:
                logger.warning("Auto-reload failed for '%s': %s", plugin_id, exc)

    async def stop(self) -> None:
        """Stop watching."""
        if self._observer is not None:
            self._observer.stop()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._observer.join(timeout=2.0)
            )
            self._observer = None
        logger.info("Plugin file watcher stopped")
