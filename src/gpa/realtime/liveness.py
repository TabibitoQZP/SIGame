"""Turn loss of the controlled browser/page into an immediate runtime failure."""


def watch_browser(browser, page, adapter):
    def fail(reason):
        if adapter.closing:
            return
        adapter.closing = True
        adapter.error = adapter.error or reason
        adapter.emit("adapter_error", error_type=adapter.error)
        adapter.finished.set()

    browser.on("disconnected", lambda *_: fail("browser_disconnected"))
    page.on("crash", lambda *_: fail("page_crashed"))
    page.on("close", lambda *_: fail("page_closed"))
