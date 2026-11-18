"""Container-local TCP listener forwarding only to its mounted Unix model bridge."""

import select
import socket
import socketserver
import sys


def serve(path, port=18080):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as target:
                target.connect(path)
                peers = {self.request: target, target: self.request}
                while True:
                    ready, _, _ = select.select(list(peers), [], [], 10)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        peers[source].sendall(data)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", port), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 18080)
