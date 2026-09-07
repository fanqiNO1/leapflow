"""Tests for LeapAppActor's routing policy and polling helpers."""

import pytest

pytest.importorskip("cua_sandbox")  # leapspace dependency group only

from leapspace.app_space.actor import ActionResult, LeapAppActor


def make_actor() -> LeapAppActor:
    # __init__ only stores the sandbox; the helpers under test never touch it
    # because list_windows / get_window_state are stubbed per test.
    return LeapAppActor(sandbox=object())


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[object] = []
        self.fail = False

    async def type(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("kb down")
        self.typed.append(text)

    async def keypress(self, key: object) -> None:
        if self.fail:
            raise RuntimeError("kb down")
        self.pressed.append(key)


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple] = []
        self.scrolls: list[tuple] = []

    async def click(self, x: int, y: int, button: str = "left") -> None:
        self.clicks.append((x, y, button))

    async def scroll(self, x: int, y: int, scroll_x: int = 0, scroll_y: int = 0) -> None:
        self.scrolls.append((x, y, scroll_x, scroll_y))


class FakeSandbox:
    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()


def make_routing_actor() -> tuple[LeapAppActor, FakeSandbox, list[tuple[str, dict]]]:
    actor = LeapAppActor(sandbox=FakeSandbox())
    mcp_calls: list[tuple[str, dict]] = []

    async def call_tool(name: str, args: dict) -> ActionResult:
        mcp_calls.append((name, args))
        return ActionResult(ok=True, via="mcp", data={"echo": name})

    actor._call_mcp_tool = call_tool  # type: ignore[method-assign]
    return actor, actor.sandbox, mcp_calls  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_wait_for_window_finds_matching_title():
    actor = make_actor()
    target = {"pid": 1, "window_id": 2, "title": "LeapChat (1)"}

    async def list_windows():
        return ActionResult(ok=True, via="sdk", data={"windows": [target]})

    actor.list_windows = list_windows
    window = await actor.wait_for_window("LeapChat")
    assert window is target


@pytest.mark.asyncio
async def test_wait_for_window_retries_until_match():
    actor = make_actor()
    calls = 0

    async def list_windows():
        nonlocal calls
        calls += 1
        if calls < 3:
            return ActionResult(ok=True, via="sdk", data={"windows": []})
        return ActionResult(
            ok=True, via="sdk",
            data={"windows": [{"pid": 1, "window_id": 2, "title": "LeapChat"}]},
        )

    actor.list_windows = list_windows
    window = await actor.wait_for_window("LeapChat", poll_s=0.01)
    assert window["title"] == "LeapChat"
    assert calls == 3


@pytest.mark.asyncio
async def test_wait_for_window_accepts_bare_list_payload():
    actor = make_actor()

    async def list_windows():
        return ActionResult(
            ok=True, via="sdk",
            data=[{"pid": 1, "window_id": 2, "title": "LeapChat"}],
        )

    actor.list_windows = list_windows
    assert (await actor.wait_for_window("LeapChat"))["pid"] == 1


@pytest.mark.asyncio
async def test_wait_for_window_rejects_similar_title():
    actor = make_actor()

    async def list_windows():
        return ActionResult(
            ok=True, via="sdk",
            data={"windows": [{"pid": 1, "window_id": 2, "title": "LeapChat Pro"}]},
        )

    actor.list_windows = list_windows
    with pytest.raises(RuntimeError, match="did not appear"):
        await actor.wait_for_window("LeapChat", timeout_s=0.05, poll_s=0.01)


@pytest.mark.asyncio
async def test_wait_for_window_times_out():
    actor = make_actor()

    async def list_windows():
        return ActionResult(ok=True, via="sdk", data={"windows": []})

    actor.list_windows = list_windows
    with pytest.raises(RuntimeError, match="did not appear"):
        await actor.wait_for_window("Nope", timeout_s=0.05, poll_s=0.01)


@pytest.mark.asyncio
async def test_snapshot_tree_returns_tree_markdown():
    actor = make_actor()

    async def get_window_state(pid, window_id):
        assert (pid, window_id) == (1, 2)
        return ActionResult(ok=True, via="mcp", data={"tree_markdown": "- [0] window"})

    actor.get_window_state = get_window_state
    assert await actor.snapshot_tree(1, 2) == "- [0] window"


# ── routing policy: SDK first, driver fallback, element addressing MCP-only ──


@pytest.mark.asyncio
async def test_press_key_goes_sdk_first():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.press_key("down")
    assert result.via == "sdk"
    assert sandbox.keyboard.pressed == ["down"]
    assert mcp_calls == []


@pytest.mark.asyncio
async def test_press_key_falls_back_to_mcp_when_sdk_fails():
    actor, sandbox, mcp_calls = make_routing_actor()
    sandbox.keyboard.fail = True
    result = await actor.press_key("down")
    assert result.via == "mcp"
    assert mcp_calls[0][0] == "press_key"
    assert mcp_calls[0][1]["key"] == "down"


@pytest.mark.asyncio
async def test_press_key_raises_when_both_paths_fail():
    actor, sandbox, _ = make_routing_actor()

    async def failing_tool(name: str, args: dict) -> ActionResult:
        return ActionResult(ok=False, via="mcp", error="driver no-op")

    actor._call_mcp_tool = failing_tool  # type: ignore[method-assign]
    sandbox.keyboard.fail = True
    with pytest.raises(RuntimeError, match=r"via SDK .* and MCP \(driver no-op\)"):
        await actor.press_key("down")


@pytest.mark.asyncio
async def test_type_text_goes_sdk_first_without_element_addressing():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.type_text("Hello.")
    assert result.via == "sdk"
    assert sandbox.keyboard.typed == ["Hello."]
    assert mcp_calls == []


@pytest.mark.asyncio
async def test_type_text_element_addressing_is_driver_only():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.type_text("hi", element_index=3)
    assert result.via == "mcp"
    assert sandbox.keyboard.typed == []
    assert mcp_calls[0][0] == "type_text"
    assert mcp_calls[0][1]["element_index"] == 3


@pytest.mark.asyncio
async def test_type_text_element_addressing_failure_has_no_sdk_fallback():
    actor, sandbox, _ = make_routing_actor()

    async def failing_tool(name: str, args: dict) -> ActionResult:
        return ActionResult(ok=False, via="mcp", error="boom")

    actor._call_mcp_tool = failing_tool  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="no SDK fallback"):
        await actor.type_text("hi", element_index=3)
    assert sandbox.keyboard.typed == []


@pytest.mark.asyncio
async def test_click_element_addressing_is_driver_only():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.click(1, 2, element_index=5)
    assert result.via == "mcp"
    assert sandbox.mouse.clicks == []
    assert mcp_calls[0][0] == "click"
    assert mcp_calls[0][1]["element_index"] == 5


@pytest.mark.asyncio
async def test_click_pixel_goes_sdk_first_with_translated_coords():
    actor, sandbox, mcp_calls = make_routing_actor()

    async def to_screen(pid, window_id, x, y):
        return x + 100, y + 200

    actor._to_screen = to_screen  # type: ignore[method-assign]
    result = await actor.click(1, 2, x=10, y=20, button="right")
    assert result.via == "sdk"
    assert sandbox.mouse.clicks == [(110, 220, "right")]
    assert mcp_calls == []


@pytest.mark.asyncio
async def test_hotkey_goes_sdk_first():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.hotkey(["ctrl", "c"])
    assert result.via == "sdk"
    assert sandbox.keyboard.pressed == [["ctrl", "c"]]
    assert mcp_calls == []


@pytest.mark.asyncio
async def test_scroll_keystroke_path_is_driver_only():
    actor, sandbox, mcp_calls = make_routing_actor()
    result = await actor.scroll("down")
    assert result.via == "mcp"
    assert mcp_calls[0][0] == "scroll"
    assert mcp_calls[0][1]["direction"] == "down"


@pytest.mark.asyncio
async def test_scroll_pixel_path_goes_sdk_first():
    actor, sandbox, mcp_calls = make_routing_actor()

    async def to_screen(pid, window_id, x, y):
        return x, y

    actor._to_screen = to_screen  # type: ignore[method-assign]
    result = await actor.scroll("down", pid=1, window_id=2, x=5, y=5, amount=3)
    assert result.via == "sdk"
    assert mcp_calls == []
