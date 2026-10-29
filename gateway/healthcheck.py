"""Small loopback HTTP probe; avoid importing SSL/urllib in CPU-limited containers."""

import socket
import sys


def main():
    try:
        port = int(sys.argv[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            first_line = connection.makefile("rb").readline(256)
        return 0 if first_line.split()[1:2] == [b"200"] else 1
    except (OSError, ValueError, IndexError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
