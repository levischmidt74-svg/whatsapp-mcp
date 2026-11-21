"""Tools must not block the server's event loop while they run."""

import time

import anyio

import main


def test_slow_tool_does_not_block_other_tools(monkeypatch):
    def slow_send(recipient, message):
        time.sleep(0.5)
        return True, "sent"

    monkeypatch.setattr(main, "whatsapp_send_message", slow_send)
    monkeypatch.setattr(main, "whatsapp_search_contacts", lambda query: [])

    async def scenario():
        fast_elapsed = None

        async def slow():
            await main.mcp.call_tool("send_message", {"recipient": "123", "message": "hi"})

        async def fast():
            nonlocal fast_elapsed
            await anyio.sleep(0.05)  # let the slow call start first
            start = time.monotonic()
            await main.mcp.call_tool("search_contacts", {"query": "x"})
            fast_elapsed = time.monotonic() - start

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow)
            tg.start_soon(fast)
        return fast_elapsed

    assert anyio.run(scenario) < 0.3


def test_tools_keep_their_schema_and_stay_directly_callable(monkeypatch):
    tools = {t.name: t for t in anyio.run(main.mcp.list_tools)}
    assert "transcribe_voice_message" in tools
    assert set(tools["send_message"].inputSchema["required"]) == {"recipient", "message"}
    assert "Send a WhatsApp message" in (tools["send_message"].description or "")

    monkeypatch.setattr(main, "whatsapp_search_contacts", lambda query: [{"jid": "a"}])
    assert main.search_contacts("x") == [{"jid": "a"}]
