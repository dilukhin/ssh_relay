"""SSH к конечному узлу через независимое проверенное соединение и direct-tcpip."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from typing import Any


@dataclass(frozen=True)
class Route:
    host: str
    port: int
    user: str
    target_host: str
    target_port: int
    identity_file: str | None
    known_hosts: str | None
    ask_key_passphrase: bool

    @classmethod
    def from_args(cls, args: Any) -> Route | None:
        host = getattr(args, "via_host", None)
        names = ("via_port", "via_user", "via_target_host", "via_target_port",
                 "via_identity_file", "via_known_hosts", "via_ask_key_passphrase")
        if host is None:
            if any(getattr(args, name, None) not in (None, False) for name in names):
                raise ValueError("Параметры --via-* требуют --via-host.")
            return None
        user = getattr(args, "via_user", None)
        target_port = getattr(args, "via_target_port", None)
        port = getattr(args, "via_port", None)
        port = 22 if port is None else port
        target_host = getattr(args, "via_target_host", None)
        target_host = "127.0.0.1" if target_host is None else target_host
        if not user or target_port is None:
            raise ValueError("Для --via-host нужны --via-user и --via-target-port.")
        for value in (host, user, target_host):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("Узлы и пользователь --via-* должны быть непустыми строками.")
        for value in (port, target_port):
            if type(value) is not int or not 1 <= value <= 65535:
                raise ValueError("Порты --via-* должны быть от 1 до 65535.")
        identity = getattr(args, "via_identity_file", None)
        ask = bool(getattr(args, "via_ask_key_passphrase", False))
        if ask and not identity:
            raise ValueError("--via-ask-key-passphrase требует --via-identity-file.")
        return cls(host, port, user, target_host, target_port, identity,
                   getattr(args, "via_known_hosts", None), ask)

    def public(self) -> dict[str, Any]:
        return {"schema_version": 1, "kind": "ssh_direct_tcpip", "via_host": self.host,
                "via_port": self.port, "via_user": self.user,
                "forward_host": self.target_host, "forward_port": self.target_port}


class Transport:
    """Проверка активности охватывает оба соединения; ключ принадлежит конечному узлу."""
    def __init__(self, target: Any, intermediary: Any) -> None:
        self.target = target
        self.intermediary = intermediary

    def is_active(self) -> bool:
        return bool(self.target.is_active() and self.intermediary.is_active())

    def is_authenticated(self) -> bool:
        return bool(self.target.is_authenticated() and self.intermediary.is_authenticated())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


class Client:
    """Владеет только соединениями relay; независимый обратный туннель не закрывает."""
    def __init__(self, target: Any, intermediary: Any, channel: Any, route: Route) -> None:
        self.target = target
        self.intermediary = intermediary
        self.channel = channel
        self.route = route

    def get_transport(self) -> Transport | None:
        target = self.target.get_transport()
        intermediary = self.intermediary.get_transport()
        return Transport(target, intermediary) if target is not None and intermediary is not None else None

    def intermediary_identity(self) -> dict[str, Any] | None:
        transport = self.get_transport()
        if transport is None or not transport.is_active() or not transport.is_authenticated():
            return None
        key = transport.intermediary.get_remote_server_key()
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        return {"schema_version": 1, "remote_host": self.route.host, "remote_port": self.route.port,
                "remote_user": self.route.user, "host_key_algorithm": key.get_name(),
                "remote_host_key_sha256": "SHA256:" + digest, "trusted_known_hosts": True}

    def close(self) -> None:
        # Каждая созданная часть закрывается и при ошибке другой части.
        for resource in (self.target, self.channel, self.intermediary):
            try:
                resource.close()
            except Exception:
                pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def open_client(paramiko: Any, route: Route, *, target_host: str, target_port: int,
                target_user: str, target_known_hosts: str | None, target_identity: str | None,
                target_password: str | None, target_passphrase: str | None,
                via_identity: str | None, via_password: str | None, via_passphrase: str | None,
                keepalive: int) -> Client:
    intermediary = paramiko.SSHClient()
    target = None
    channel = None
    try:
        if route.known_hosts:
            intermediary.load_system_host_keys(route.known_hosts)
        else:
            intermediary.load_system_host_keys()
        intermediary.set_missing_host_key_policy(paramiko.RejectPolicy())
        intermediary.connect(route.host, port=route.port, username=route.user,
                             key_filename=via_identity, password=via_password, passphrase=via_passphrase,
                             look_for_keys=False, allow_agent=False, timeout=10)
        transport = intermediary.get_transport()
        if transport is None or not transport.is_active() or not transport.is_authenticated():
            raise ValueError("SSH-соединение с посредником не подтверждено.")
        transport.set_keepalive(keepalive)
        try:
            channel = transport.open_channel("direct-tcpip", (route.target_host, route.target_port),
                                             ("127.0.0.1", 0), timeout=10)
        except Exception as exc:
            raise ValueError("Посредник не открыл заданный обратный порт. Проверьте туннель и PermitOpen.") from exc
        target = paramiko.SSHClient()
        if target_known_hosts:
            target.load_system_host_keys(target_known_hosts)
        else:
            target.load_system_host_keys()
        target.set_missing_host_key_policy(paramiko.RejectPolicy())
        # hostname задаёт known_hosts identity; TCP/DNS к нему не выполняется:
        # весь SSH handshake идёт по явно созданному каналу посредника.
        target.connect(target_host, port=target_port, username=target_user, sock=channel,
                       key_filename=target_identity, password=target_password, passphrase=target_passphrase,
                       look_for_keys=False, allow_agent=False, timeout=10)
        target_transport = target.get_transport()
        if target_transport is None or not target_transport.is_active() or not target_transport.is_authenticated():
            raise ValueError("SSH-соединение с конечным узлом не подтверждено.")
        target_transport.set_keepalive(keepalive)
        return Client(target, intermediary, channel, route)
    except Exception:
        for resource in (target, channel, intermediary):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        raise
