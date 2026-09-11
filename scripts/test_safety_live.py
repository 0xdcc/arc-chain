"""Read-only transport policy for the explicitly authorized three live tests."""

from __future__ import annotations

import datetime
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

READ_METHODS = frozenset(
    {
        "web3_clientVersion",
        "eth_chainId",
        "eth_blockNumber",
        "eth_gasPrice",
        "eth_call",
        "eth_getLogs",
        "eth_getBlockByNumber",
    }
)


class LiveRPCFailure(BaseException):
    """Stop immediately, including clients which catch Exception to retry RPCs."""


def validate_request(url: str, method: str, body: Any, endpoints: set[str]) -> list[dict]:
    """Authorize exact public endpoints and an explicit list of read-only methods."""
    parsed = urlsplit(url)
    if method != "POST" or url.rstrip("/") not in endpoints or parsed.username or parsed.password:
        raise LiveRPCFailure("Read-only RPC endpoint/method rejected")
    payload = json.loads(body)
    calls = payload if isinstance(payload, list) else [payload]
    if not calls or any(
        not isinstance(call, dict)
        or call.get("method") not in READ_METHODS
        or call.get("jsonrpc") != "2.0"
        or "id" not in call
        for call in calls
    ):
        raise LiveRPCFailure("Non-read-only or malformed RPC request rejected")
    return calls


class LivePolicy:
    """Forward real requests, record raw evidence, and latch the first failure."""

    def __init__(self, source: Path, output: Path, endpoints: set[str]) -> None:
        self.source = source
        self.output = output
        self.endpoints = endpoints
        self.hosts = {urlsplit(endpoint).hostname for endpoint in endpoints}
        self.failed = False
        self.active = False
        self.addresses: set[str] = set()
        self.original_connect = socket.socket.connect
        self.original_socket_init = socket.socket.__init__
        self.original_getaddrinfo = socket.getaddrinfo
        self.read_roots = [source, output, Path(sys.base_prefix).resolve(), Path("/usr")]
        self.read_roots.append(Path(sys.executable).parent.parent.resolve())

    def record(self, **record: Any) -> None:
        """Append complete RPC evidence with a UTC timestamp."""
        record["time_utc"] = datetime.datetime.now(datetime.UTC).isoformat()
        with (self.output / "live-rpc.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def audit(self, event: str, args: tuple) -> None:
        """Deny key reads, external filesystem mutation and process execution in live workers."""
        if event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            if (
                path.name == ".env"
                or path.name.startswith(".env.")
                or path.suffix in (".key", ".pem")
                or "keystore" in path.name.lower()
                or any(part in (".secrets", ".hermes", ".codex", ".ssh") for part in path.parts)
            ):
                raise LiveRPCFailure("Credential file access prohibited")
            writing = args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            roots = [self.output] if writing else self.read_roots
            if not any(path.is_relative_to(root) for root in roots):
                raise LiveRPCFailure(f"Live filesystem access outside approved roots: {path}")
        if event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.chmod"):
            paths = args[:2] if event == "os.rename" else args[:1]
            for value in paths:
                if not isinstance(value, (str, bytes, os.PathLike)):
                    raise LiveRPCFailure("Unresolved filesystem mutation prohibited")
                if not Path(os.fsdecode(value)).resolve().is_relative_to(self.output):
                    raise LiveRPCFailure("Filesystem mutation outside live temporary root")
        if event in (
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
            "os.fork",
            "os.kill",
        ):
            raise LiveRPCFailure("Processes/signals prohibited in live verification")

    def activate_transport(self) -> None:
        """Enable only authorized real HTTP transport after the ordinary conftest is loaded."""
        import requests
        from eth_account import Account
        from urllib3.util import Retry

        policy = self
        original_send = requests.Session.send
        original_request = requests.Session.request

        def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            if not policy.active or host not in policy.hosts or int(port) != 443:
                raise LiveRPCFailure("DNS outside authorized RPC request prohibited")
            addresses = policy.original_getaddrinfo(host, port, *args, **kwargs)
            policy.addresses.update(item[4][0] for item in addresses if isinstance(item[4][0], str))
            return addresses

        def connect(sock: socket.socket, address: Any) -> None:
            if (
                not policy.active
                or sock.family == socket.AF_UNIX
                or address[0] not in policy.addresses
                or address[1] != 443
            ):
                raise LiveRPCFailure("Socket outside authorized read-only RPC prohibited")
            policy.original_connect(sock, address)

        def request(session: Any, *args: Any, **kwargs: Any) -> Any:
            session.trust_env = False
            session.auth = None
            session.proxies.clear()
            return original_request(session, *args, **kwargs)

        def send(session: Any, prepared: Any, **kwargs: Any) -> Any:
            if policy.failed:
                raise LiveRPCFailure("Live RPC halted after first failure")
            calls = validate_request(prepared.url, prepared.method, prepared.body, policy.endpoints)
            session.get_adapter(prepared.url).max_retries = Retry(
                total=0, connect=0, read=0, redirect=0, status=0
            )
            kwargs.update(allow_redirects=False, proxies={}, timeout=20)
            policy.record(event="request", url=prepared.url, payload=calls)
            try:
                policy.active = True
                response = original_send(session, prepared, **kwargs)
                policy.record(event="response", status=response.status_code, body=response.text)
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"HTTP {response.status_code}: {response.text}")
                decoded = response.json()
                replies = decoded if isinstance(decoded, list) else [decoded]
                if len(replies) != len(calls) or any(
                    not isinstance(reply, dict) or "error" in reply or "result" not in reply
                    for reply in replies
                ):
                    raise ValueError(f"RPC returned error or malformed response: {response.text}")
                for call in calls:
                    reply = next(
                        (reply for reply in replies if reply.get("id") == call["id"]), None
                    )
                    if reply is None:
                        raise ValueError("RPC response id mismatch")
                    if call["method"] in {"eth_chainId", "eth_blockNumber", "eth_gasPrice"}:
                        print(f"LIVE_VALUE {call['method']}={int(reply['result'], 16)}", flush=True)
                return response
            except BaseException as error:
                policy.failed = True
                policy.record(
                    event="first_failure", error_type=type(error).__name__, error=str(error)
                )
                print(
                    f"LIVE_FIRST_RPC_FAILURE {datetime.datetime.now(datetime.UTC).isoformat()} {type(error).__name__}: {error}",
                    flush=True,
                )
                raise LiveRPCFailure(str(error)) from error
            finally:
                policy.active = False

        def no_signing(*args: Any, **kwargs: Any) -> Any:
            raise LiveRPCFailure("Signing/key use prohibited in live verification")

        for name in ("sign_transaction", "sign_message", "sign_typed_data", "from_key"):
            setattr(Account, name, no_signing)
        for target, name, implementation in (
            (socket.socket, "__init__", self.original_socket_init),
            (socket.socket, "connect", connect),
            (socket, "getaddrinfo", getaddrinfo),
            (requests.Session, "request", request),
            (requests.Session, "send", send),
        ):
            setattr(target, name, implementation)
