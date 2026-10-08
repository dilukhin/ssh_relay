"""Настоящий SSH reverse forwarding; слушатели и назначения только loopback теста."""
from __future__ import annotations

import socket
import threading
import paramiko


def bridge(left, right):
    """Два направления без shell и без сохранения передаваемых данных."""
    def pump(source, destination):
        try:
            while True:
                data = source.recv(32768)
                if not data:
                    break
                destination.sendall(data)
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            for stream in (source, destination):
                if isinstance(stream, socket.socket):
                    # close из другого потока не прерывает recv на Linux.
                    # shutdown завершает TCP и будит второй поток передачи.
                    try:
                        stream.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                try:
                    stream.close()
                except (OSError, EOFError, paramiko.SSHException):
                    pass
    threading.Thread(target=pump, args=(left, right), daemon=True).start()
    threading.Thread(target=pump, args=(right, left), daemon=True).start()


class ForwardServer(paramiko.ServerInterface):
    def __init__(self, owner, transport):
        self.owner = owner
        self.transport = transport
        self.destinations = {}
        self.reverse_listener = None

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return (paramiko.AUTH_SUCCESSFUL if username == "via-user" and password == "via-test-password"
                else paramiko.AUTH_FAILED)

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.owner.requests.append(destination)
        if self.owner.block_forward or destination != ("127.0.0.1", self.owner.forward_port):
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        try:
            self.destinations[chanid] = socket.create_connection(destination, timeout=1)
            self.destinations[chanid].settimeout(None)
            return paramiko.OPEN_SUCCEEDED
        except OSError:
            return paramiko.OPEN_FAILED_CONNECT_FAILED

    def check_port_forward_request(self, address, port):
        if address != "127.0.0.1" or port != 0 or self.reverse_listener is not None:
            return False
        listener = socket.socket()
        listener.bind((address, 0))
        listener.listen(8)
        listener.settimeout(0.1)
        self.reverse_listener = listener
        self.owner.reverse_transport = self.transport
        self.owner.forward_port = listener.getsockname()[1]
        def accept():
            while self.transport.is_active() and not self.owner.stopped.is_set():
                try:
                    connection, source = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    channel = self.transport.open_forwarded_tcpip_channel(
                        source, (address, self.owner.forward_port))
                    bridge(connection, channel)
                except Exception:
                    connection.close()
            listener.close()
        threading.Thread(target=accept, daemon=True).start()
        return self.owner.forward_port

    def close(self):
        if self.reverse_listener is not None:
            self.reverse_listener.close()
        for destination in self.destinations.values():
            destination.close()


class ReverseSSHFixture:
    def __init__(self, key, target_port):
        self.key = key
        self.target_port = target_port
        self.stopped = threading.Event()
        self.transports = []
        self.requests = []
        self.reverse_transport = None
        self.forward_port = 0
        self.block_forward = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.reverse_client = None

    def start(self, known_hosts):
        def serve(connection):
            transport = paramiko.Transport(connection)
            self.transports.append(transport)
            server = ForwardServer(self, transport)
            try:
                transport.add_server_key(self.key)
                transport.start_server(server=server)
                while transport.is_active() and not self.stopped.is_set():
                    channel = transport.accept(0.1)
                    if channel is not None:
                        bridge(channel, server.destinations[channel.get_id()])
            except (OSError, EOFError, paramiko.SSHException):
                pass
            finally:
                server.close()
                transport.close()
        def accept():
            while not self.stopped.is_set():
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=serve, args=(connection,), daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()
        keys = paramiko.HostKeys()
        keys.add(f"[127.0.0.1]:{self.port}", self.key.get_name(), self.key)
        keys.save(str(known_hosts))
        self.reverse_client = paramiko.SSHClient()
        self.reverse_client.load_system_host_keys(str(known_hosts))
        self.reverse_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.reverse_client.connect("127.0.0.1", port=self.port, username="via-user",
                                    password="via-test-password", look_for_keys=False, allow_agent=False)
        def forwarded(channel, _origin, _server):
            def connect():
                try:
                    target = socket.create_connection(("127.0.0.1", self.target_port), timeout=1)
                    target.settimeout(None)
                    bridge(channel, target)
                except OSError:
                    channel.close()
            threading.Thread(target=connect, daemon=True).start()
        self.reverse_client.get_transport().request_port_forward("127.0.0.1", 0, handler=forwarded)

    def drop_relay_connections(self):
        for transport in list(self.transports):
            if transport is not self.reverse_transport:
                transport.close()

    def stop_reverse(self):
        if self.reverse_client is not None:
            self.reverse_client.close()

    def stop(self):
        self.stopped.set()
        self.listener.close()
        self.stop_reverse()
        for transport in self.transports:
            transport.close()
