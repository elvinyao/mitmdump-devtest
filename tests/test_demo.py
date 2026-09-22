import asyncio
import json
import signal
import sys


async def test_documented_demo_with_real_curl_and_sigterm():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "examples/demo.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert line, (await process.stderr.read()).decode()
        assert json.loads(line)["event"] == "ready"

        async def curl(*args):
            command = await asyncio.create_subprocess_exec(
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "3",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, error = await command.communicate()
            assert command.returncode == 0, error.decode()
            return output.decode()

        statuses = [
            await curl(
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-H",
                "X-Test-Run-ID: demo",
                "http://127.0.0.1:8080/retry",
            )
            for _ in range(3)
        ]
        assert statuses == ["429", "429", "200"]
        state = json.loads(
            await curl(
                "-H", "Authorization: Bearer local-demo-token", "http://127.0.0.1:9090/state"
            )
        )
        assert state["counters"][0]["count"] == 3
        reset = await curl(
            "-X",
            "POST",
            "-H",
            "Authorization: Bearer local-demo-token",
            "-H",
            "Content-Type: application/json",
            "-d",
            '{"scope":"demo"}',
            "http://127.0.0.1:9090/reset",
        )
        assert json.loads(reset) == {"reset": 1}
        assert json.loads(await curl("http://127.0.0.1:8081/echo"))["service"] == "inventory"
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
