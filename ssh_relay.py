#!/usr/bin/env python3
"""
ssh_relay.py — Interactive SSH relay for opencode.

Usage:
  python ssh_relay.py daemon --host HOST --user USER
  python ssh_relay.py exec "command"
  python ssh_relay.py status
  python ssh_relay.py stop
"""

import argparse
import atexit
import getpass
import json
import os
import socket
import subprocess
import sys
import threading
import uuid

try:
    import paramiko
except ImportError:
    print("paramiko is required. Install: pip install paramiko", file=sys.stderr)
    sys.exit(1)

SESSION_FILE = os.path.join(os.getcwd(), ".ssh_relay_session.json")
BUFFER_SIZE = 1 << 20


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_until_eof(sock):
    data = b""
    while True:
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            break
        data += chunk
    return data.decode()


def daemon(args):
    password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            args.host, port=args.port, username=args.user,
            password=password, look_for_keys=False,
            allow_agent=False, timeout=10,
        )
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    auth_token = str(uuid.uuid4())
    daemon_port = find_free_port()
    pid = os.getpid()

    session = {
        "host": args.host, "port": args.port, "user": args.user,
        "daemon_port": daemon_port, "auth_token": auth_token, "pid": pid,
    }
    with open(SESSION_FILE, "w") as f:
        json.dump(session, f)

    def cleanup():
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        client.close()
    atexit.register(cleanup)

    lock = threading.Lock()

    def handle_client(conn):
        try:
            raw = read_until_eof(conn)
            if not raw:
                return
            req = json.loads(raw)
            if req.get("auth_token") != auth_token:
                conn.sendall(json.dumps({"error": "unauthorized"}).encode())
                return

            command = req.get("cmd", "")
            if not command:
                conn.sendall(json.dumps({"error": "empty command"}).encode())
                return

            with lock:
                stdin, stdout, stderr = client.exec_command(command, get_pty=True)
                exit_code = stdout.channel.recv_exit_status()
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")

            conn.sendall(json.dumps({
                "output": out,
                "error": err,
                "exit_code": exit_code,
            }).encode())
        except Exception as e:
            try:
                conn.sendall(json.dumps({"error": str(e)}).encode())
            except Exception:
                pass
        finally:
            conn.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", daemon_port))
    server.listen(5)

    print(f"Connected: {args.user}@{args.host}:{args.port}")
    print(f"Daemon listening on 127.0.0.1:{daemon_port}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle_client, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.close()
        cleanup()


def exec_cmd(args):
    if not os.path.exists(SESSION_FILE):
        print("No session. Start one: ssh_relay.py daemon ...", file=sys.stderr)
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session = json.load(f)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", session["daemon_port"]))
        sock.sendall(json.dumps({
            "auth_token": session["auth_token"],
            "cmd": args.command,
        }).encode())
        sock.shutdown(socket.SHUT_WR)

        result = json.loads(read_until_eof(sock))

        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

        if result.get("output"):
            sys.stdout.write(result["output"])
        if result.get("error"):
            sys.stderr.write(result["error"])

        return result.get("exit_code", 0)
    except ConnectionRefusedError:
        print("Daemon unreachable. Session expired?", file=sys.stderr)
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        sys.exit(1)


def stop(args):
    if not os.path.exists(SESSION_FILE):
        print("No session.", file=sys.stderr)
        return

    with open(SESSION_FILE) as f:
        session = json.load(f)

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(session["pid"])],
                       capture_output=True)
    else:
        os.kill(session["pid"], 9)

    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    print("Session stopped.")


def status(args):
    if not os.path.exists(SESSION_FILE):
        print("No active session.")
        return

    with open(SESSION_FILE) as f:
        session = json.load(f)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(("127.0.0.1", session["daemon_port"]))
        sock.close()
        print(f"Active: {session['user']}@{session['host']}:{session['port']}")
        print(f"Port: {session['daemon_port']}")
    except Exception:
        print("Session file exists but daemon is not running.")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive SSH relay for opencode",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="Start SSH relay (prompts for password)")
    d.add_argument("--host", required=True)
    d.add_argument("--port", type=int, default=22)
    d.add_argument("--user", "-u", default=getpass.getuser())

    e = sub.add_parser("exec", help="Execute command via relay")
    e.add_argument("command", help="Command to run on remote host")

    sub.add_parser("stop", help="Stop the daemon")
    sub.add_parser("status", help="Check daemon status")

    args = parser.parse_args()

    if args.command == "daemon":
        daemon(args)
    elif args.command == "exec":
        sys.exit(exec_cmd(args))
    elif args.command == "stop":
        stop(args)
    elif args.command == "status":
        status(args)


if __name__ == "__main__":
    main()
