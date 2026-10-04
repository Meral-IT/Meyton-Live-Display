"""Check the actual SSE generator without opening a long-lived HTTP connection."""

import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.main import Runtime, create_app
from app.resource_saver import read_json
from test_app import target


class EventChecks(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_streams_publish_shared_viewer_count(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app()
            runtime = app.state.runtime = Runtime(Path(directory) / "profiles.json")
            endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")
            streams = []
            for expected in (1, 2):
                request = Request({"type": "http", "app": app})
                request.is_disconnected = AsyncMock(return_value=False)
                stream = (await endpoint(request, profile="alles", lane=1)).body_iterator
                await anext(stream)
                streams.append(stream)
                self.assertEqual(read_json(runtime.viewer_presence_path)["viewer_count"], expected)
            await streams[0].aclose()
            self.assertEqual(read_json(runtime.viewer_presence_path)["viewer_count"], 1)
            await streams[1].aclose()
            self.assertEqual(read_json(runtime.viewer_presence_path)["viewer_count"], 0)

    async def test_meaningful_updates_heartbeats_reconnect_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app()
            runtime = app.state.runtime = Runtime(Path(directory) / "profiles.json")
            runtime.ranges.apply(target())
            endpoint = next(route.endpoint for route in app.routes if route.path == "/api/events")
            request = Request({"type": "http", "app": app})
            request.is_disconnected = AsyncMock(return_value=False)
            response = await endpoint(request, profile="alles", lane=1)
            stream = response.body_iterator
            actions, timeouts = deque(), []

            async def wait(awaitable, timeout):
                awaitable.close()
                timeouts.append(timeout)
                action = actions.popleft()
                if action is None:
                    raise TimeoutError
                action()
                runtime.notify()
                await asyncio.sleep(0.001)

            def snapshot(event):
                self.assertTrue(event.startswith("event: snapshot\n"))
                return json.loads(event.split("data: ", 1)[1])

            with patch("app.main.asyncio.wait_for", side_effect=wait):
                initial = snapshot(await anext(stream))
                self.assertEqual(initial["rows"][0][0]["target"]["shot_count"], 1)
                self.assertEqual(read_json(runtime.viewer_presence_path)["viewer_count"], 1)

                other = target(2, 201)
                other.update(lane=51, id=-51)
                actions.extend([
                    lambda: runtime.status["db"].update(last_check_at="later"),
                    lambda: runtime.ranges.apply(other),
                    lambda: runtime.ranges.apply(target(2, 201)),
                ])
                updated = snapshot(await anext(stream))
                self.assertEqual(updated["rows"][0][0]["target"]["shot_count"], 2)
                self.assertEqual(len(timeouts), 3)  # Timestamp-only and other-lane updates were skipped.
                self.assertGreater(timeouts[0], timeouts[1])
                self.assertGreater(timeouts[1], timeouts[2])  # Unchanged notifications don't postpone heartbeats.

                actions.extend([None, None])
                self.assertEqual(await anext(stream), ": heartbeat\n\n")
                self.assertEqual(await anext(stream), ": heartbeat\n\n")  # No full snapshot after a timeout.

                for change in (
                    lambda: runtime.status["db"].update(state="connected", message="ok"),
                    lambda: runtime.status["db"].update(state="disconnected", message="offline"),
                    lambda: setattr(runtime.profiles.get("alles"), "name", "Updated"),
                    lambda: runtime.sponsors.images.append({"id": "test", "url": "/test.png"}),
                    lambda: setattr(runtime, "id", "new-runtime"),
                    lambda: runtime.ranges.set_occupancy({1: {"state": "free", "shooter": ""}}),
                ):
                    actions.append(change)
                    snapshot(await anext(stream))

                await stream.aclose()
                self.assertFalse(runtime.listeners)
                self.assertEqual(read_json(runtime.viewer_presence_path)["viewer_count"], 0)
                reconnect = (await endpoint(request, profile="alles", lane=1)).body_iterator
                self.assertEqual(snapshot(await anext(reconnect))["runtime_id"], "new-runtime")
                actions.append(lambda: setattr(runtime.profiles, "profiles", runtime.profiles.profiles[1:]))
                self.assertEqual(await anext(reconnect), "event: deleted\ndata: {}\n\n")
                with self.assertRaises(StopAsyncIteration):
                    await anext(reconnect)
                self.assertFalse(runtime.listeners)


if __name__ == "__main__":
    unittest.main()
