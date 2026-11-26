"""Bounded command output and an owned Windows process tree."""

import asyncio
import os
import subprocess


def own_windows_job(pid):
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        handle = win32api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
    except BaseException:
        job.Close()
        raise
    return job


async def execute(argv, cwd, env, timeout):
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    job = None
    data = bytearray()

    async def drain():
        while chunk := await process.stdout.read(4096):
            if len(data) < 64000:
                data.extend(chunk[: 64000 - len(data)])
        await process.wait()

    try:
        if os.name == "nt":
            job = own_windows_job(process.pid)
        await asyncio.wait_for(drain(), timeout)
        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "output": data.decode("utf-8", errors="replace"),
            "truncated": len(data) == 64000,
        }
    finally:
        if job:
            job.Close()
        elif process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
