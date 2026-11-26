import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import test_control as shared

from jui import control_voice_processing as processing
from jui.web import app

center = shared.center
store = shared.store


def test_chain_publishes_private_output_paths_and_can_cancel(
    center, monkeypatch, tmp_path
):
    source = tmp_path / "会议.wav"
    source.write_bytes(b"source audio")
    output = tmp_path / "结果"
    output.mkdir()
    monkeypatch.setattr(app.state, "control", center, raising=False)
    monkeypatch.setattr(
        processing, "installation", lambda: {"inworld_ready": True, "uvr_ready": True}
    )

    async def scenario():
        started = asyncio.Event()
        sleep_checked = False

        async def wait_for_model_sleep():
            nonlocal sleep_checked
            assert center.gpu_lock.locked()
            sleep_checked = True

        async def worker(script, args, on_line=None):
            flags = dict(zip(args[::2], args[1::2]))
            if script == processing.UVR:
                assert not center.gpu_lock.locked()
                assert flags["--input"] == source
                on_line("UVR_DEVICE cpu")
                on_line("UVR_PROGRESS 1 1")
                for flag in ("--vocals", "--instrumental"):
                    flags[flag].write_bytes(b"x" * 100)
                return ""
            flags["--voice"].write_bytes(b"x" * 100)
            if flags["--device"] == "cpu":
                started.set()
                await asyncio.Future()
            assert center.gpu_lock.locked() and sleep_checked
            assert flags["--input"].name == "uvr-vocals.wav"
            assert "--background" not in flags
            return json.dumps(
                {"device": "cuda", "sample_rate": 48000, "sample_count": 24}
            )

        monkeypatch.setattr(processing, "run_worker", worker)
        monkeypatch.setattr(
            app.state,
            "voice",
            SimpleNamespace(wait_for_model_sleep=wait_for_model_sleep),
            raising=False,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            url = "/api/v1/voice-processing"
            body = {
                "input_path": str(source),
                "output_dir": str(output),
                "mode": "uvr_inworld",
                "save_background": False,
            }
            assert (await client.get(url + "/status")).status_code == 401
            client.headers["Authorization"] = "Bearer " + center.store.desktop_token()
            response = await client.post(url + "/jobs", json=body)
            assert response.status_code == 202
            identity = response.json()["id"]
            path = url + "/jobs/" + identity
            job = (await client.get(path)).json()
            assert job["status"] == "completed" and job["uvr_device"] == "cpu"
            assert job["device"] == "cuda" and job["background_path"] is None
            assert Path(job["output_dir"]).parent == output
            assert {item.name for item in Path(job["output_dir"]).iterdir()} == {
                "voice.wav",
                "uvr-vocals.wav",
                "uvr-instrumental.wav",
            }
            events = [
                event["data"]
                for event in center.store.events()
                if event["kind"] == "voice_processing"
            ]
            assert events == [
                {"id": identity, "revision": revision}
                for revision in range(1, job["revision"] + 1)
            ]

            response = await client.post(
                url + "/jobs", json={**body, "mode": "inworld", "device": "cpu"}
            )
            identity = response.json()["id"]
            await asyncio.wait_for(started.wait(), 5)
            assert (await client.get("/api/v1/state")).json()[
                "voice_processing_running"
            ] == [identity]
            assert (await client.post(url + "/jobs", json=body)).status_code == 409
            path = url + "/jobs/" + identity
            assert (await client.post(path + "/cancel")).json()[
                "status"
            ] == "cancelling"
            assert (await client.get(path)).json()["status"] == "cancelled"
            assert [p.name for p in output.iterdir()] == [Path(job["output_dir"]).name]
            assert source.read_bytes() == b"source audio"

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows job object handles process trees")
def test_cancelling_worker_also_stops_its_subprocess(monkeypatch, tmp_path):
    import win32api
    import win32event

    monkeypatch.setattr(processing, "DIRECTORY", tmp_path)
    monkeypatch.setattr(processing, "PYTHON", Path(sys.executable))
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    async def scenario():
        child_pid = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            processing.run_worker(
                script, (), lambda line: child_pid.set_result(int(line))
            )
        )
        handle = None
        try:
            pid = await asyncio.wait_for(child_pid, 10)
            handle = win32api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if handle:
                try:
                    assert (
                        win32event.WaitForSingleObject(handle, 5000)
                        == win32event.WAIT_OBJECT_0
                    )
                finally:
                    handle.Close()

    asyncio.run(scenario())
