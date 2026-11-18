"""Select HTTP proxies for local and remote endpoints."""

import urllib.request
from urllib.parse import urlsplit

import httpx


def client(url, *, timeout):
    target = urlsplit(url)
    host = target.hostname or ""
    proxy = None
    if host not in {"127.0.0.1", "localhost", "::1"} and not urllib.request.proxy_bypass(host):
        proxies = urllib.request.getproxies()
        proxy = proxies.get(target.scheme) or proxies.get("all")
        if proxy and urlsplit(proxy).scheme not in {"http", "https"}:
            raise ValueError(
                "configure HTTP_PROXY/HTTPS_PROXY with an http(s) URL; SOCKS proxies require a separate client extension"
            )
    return httpx.Client(timeout=timeout, follow_redirects=False, proxy=proxy, trust_env=False)
