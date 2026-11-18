"""Separate connection and response timeouts."""

import http.client
import urllib.request

CONNECT_SECONDS = 2


class ConnectTimeout:
    def connect(self):
        read_timeout = self.timeout
        self.timeout = min(CONNECT_SECONDS, read_timeout)
        try:
            super().connect()
        finally:
            self.timeout = read_timeout
        self.sock.settimeout(read_timeout)


class HTTPConnection(ConnectTimeout, http.client.HTTPConnection):
    pass


class HTTPSConnection(ConnectTimeout, http.client.HTTPSConnection):
    pass


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(HTTPConnection, request)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(HTTPSConnection, request, context=self._context)
