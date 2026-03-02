"""Shared async downloader with semaphore-based concurrency."""

import asyncio
import os

import aiohttp


async def download_file(session, sem, url, out_path, headers=None):
    """Download a single file. Skips if already exists. Returns True on success."""
    if os.path.exists(out_path):
        return True
    async with sem:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(out_path, "wb") as f:
                        f.write(data)
                    return True
                return False
        except Exception:
            return False


async def download_pages(tasks, session_headers=None, max_concurrent=10, progress=None, progress_task=None):
    """Download multiple files concurrently.

    Args:
        tasks: list of (url, out_path) or (url, out_path, extra_headers) tuples
        session_headers: default headers for the aiohttp session
        max_concurrent: semaphore limit
        progress: Rich Progress instance (optional)
        progress_task: Rich task ID for progress updates (optional)

    Returns:
        list of indices that failed
    """
    sem = asyncio.Semaphore(max_concurrent)
    failed = []

    async def do_one(session, idx, url, out_path, extra_headers):
        ok = await download_file(session, sem, url, out_path, headers=extra_headers)
        if not ok:
            failed.append(idx)
        if progress and progress_task is not None:
            progress.update(progress_task, advance=1)

    async with aiohttp.ClientSession(headers=session_headers) as session:
        coros = []
        for idx, task in enumerate(tasks):
            if len(task) == 3:
                url, out_path, extra_headers = task
            else:
                url, out_path = task
                extra_headers = None
            coros.append(do_one(session, idx, url, out_path, extra_headers))

        for coro in asyncio.as_completed(coros):
            await coro

    return failed
