"""Official lifecycle calls, including late native readiness and API-requested reloads."""

import asyncio
import time
from urllib.parse import parse_qsl, urlsplit

INIT = """async () => {
 const api=window.gameAPI;
 if(!api || typeof api.getState!=='function') return {ok:false};
 if(typeof api.init!=='function') return {ok:true};
 const result=await api.init({});
 return result == null ? {ok:true} : result;
}"""
RESET = """async () => {
 return await window.gameAPI.reset({});
}"""


def task_url_identity(url):
    value = urlsplit(url)
    query = tuple(
        sorted(
            (key, item)
            for key, item in parse_qsl(value.query, keep_blank_values=True)
            if key not in {"direct", "autostart"}
        )
    )
    return value.scheme, value.netloc, value.path, query, value.fragment


async def initialize(page, timeout=45):
    deadline = time.monotonic() + timeout
    result = None
    async with asyncio.timeout(timeout):
        while time.monotonic() < deadline:
            try:
                result = await page.evaluate(INIT)
            except Exception as error:
                if "Execution context was destroyed" not in str(error):
                    raise
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(0.1)
                continue
            if result is True or isinstance(result, dict) and result.get("ok") is True:
                return result
            await asyncio.sleep(0.2)
    raise RuntimeError("official_initialization_failed:" + str(result))


async def reset(page):
    url = page.url
    capabilities = await page.evaluate("window.gameAPI?.capabilities || {}")
    navigation = asyncio.create_task(page.wait_for_event("domcontentloaded", timeout=45000))
    await asyncio.sleep(0)
    try:
        try:
            result = await page.evaluate(RESET)
        except Exception as error:
            if not capabilities.get(
                "supports_reload_reset"
            ) or "Execution context was destroyed" not in str(error):
                raise
            result = {"ok": True, "method": "reload", "navigation_interrupted_response": True}
        if result is not True and not (isinstance(result, dict) and result.get("ok") is True):
            raise RuntimeError("official_gameworld_reset_failed:" + str(result))
        method = result.get("method") if isinstance(result, dict) else None
        if (
            method == "reload"
            or not method
            and capabilities.get("supports_reload_reset")
            and not capabilities.get("supports_inplace_reset")
        ):
            await navigation
            await initialize(page)
    finally:
        navigation.cancel()
        await asyncio.gather(navigation, return_exceptions=True)
    if task_url_identity(page.url) != task_url_identity(url):
        raise RuntimeError("official_reset_changed_task_url")
    return True
