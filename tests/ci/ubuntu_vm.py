"""Одноразовая Ubuntu в QEMU; только испытательная инфраструктура GitHub Actions."""

import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import paramiko

PASSWORD = "relay-ci-artificial-password-52"


class UbuntuVM:
    def __init__(self, root, release):
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise RuntimeError("Требуется одноразовая машина GitHub Actions")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.release = release
        self.port = 22252
        self.process = None
        self.http = None
        self.logs = []
        self.known = self.root / "known_hosts"

    def download(self, url, destination):
        with urllib.request.urlopen(url, timeout=90) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)

    def start(self):
        name = f"{self.release}-server-cloudimg-amd64.img"
        base = f"https://cloud-images.ubuntu.com/{self.release}/current/"
        checksums = self.root / "SHA256SUMS"
        self.download(base + "SHA256SUMS", checksums)
        expected = next(line.split()[0] for line in checksums.read_text().splitlines()
                        if line.split()[-1].lstrip("*") == name)
        image = self.root / name
        self.download(base + name, image)
        with image.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError("Не совпала контрольная сумма образа Ubuntu")
        print(f"Образ Ubuntu {self.release}: SHA256={actual}", flush=True)
        key = paramiko.RSAKey.generate(2048)
        key_file = self.root / "host-key"
        key.write_private_key_file(str(key_file))
        public = key.get_name() + " " + key.get_base64()
        self.known.write_text(f"[127.0.0.1]:{self.port} {public}\n", encoding="utf-8")
        seed = self.root / "seed"
        seed.mkdir()
        config = {
            "users": [{"name": "relayci", "shell": "/bin/sh", "lock_passwd": False},
                      {"name": "ciadmin", "shell": "/bin/sh", "lock_passwd": False,
                       "sudo": "ALL=(ALL) NOPASSWD:ALL"}],
            "chpasswd": {"expire": False, "list": f"relayci:{PASSWORD}\nciadmin:{PASSWORD}"},
            "ssh_pwauth": True,
            "ssh_keys": {"rsa_private": key_file.read_text(), "rsa_public": public},
            "write_files": [
                {"path": "/etc/sudoers.d/relayci", "permissions": "0440",
                 "content": "relayci ALL=(root) PASSWD: ALL\nDefaults:relayci timestamp_timeout=0\n"},
                {"path": "/etc/ssh/sshd_config.d/00-relay-ci.conf", "permissions": "0600",
                 "content": "PasswordAuthentication yes\nKbdInteractiveAuthentication no\nHostKey /etc/ssh/ssh_host_rsa_key\n"}],
            "runcmd": [["systemctl", "restart", "ssh"], ["touch", "/run/relay-ci-ready"]],
        }
        (seed / "user-data").write_text("#cloud-config\n" + json.dumps(config), encoding="utf-8")
        (seed / "meta-data").write_text(f"instance-id: {uuid.uuid4()}\nlocal-hostname: relay-ci-ubuntu\n")
        (seed / "vendor-data").write_text("{}\n")
        (seed / "network-config").write_text("version: 2\nethernets:\n  ci:\n    match:\n      name: 'e*'\n    dhcp4: true\n")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(seed))
        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        qemu = shutil.which("qemu-system-x86_64") or str(Path(os.environ["ProgramFiles"]) / "qemu/qemu-system-x86_64.exe")
        qemu_img = str(Path(qemu).with_name("qemu-img.exe" if os.name == "nt" else "qemu-img"))
        subprocess.run([qemu_img, "resize", str(image), "20G"], check=True)
        args = [qemu, "-accel", "tcg,thread=multi", "-machine", "q35", "-m", "2048", "-smp", "4",
                "-display", "none", "-serial", "file:" + str(self.root / "serial.log"),
                "-monitor", "none", "-qmp", "tcp:127.0.0.1:44452,server=on,wait=off", "-drive", f"file={image},format=qcow2,if=virtio",
                "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{self.port}-:22",
                "-device", "virtio-net-pci,netdev=net0",
                "-smbios", f"type=1,serial=ds=nocloud-net;s=http://10.0.2.2:{self.http.server_port}/"]
        self.qemu_log = (self.root / "qemu.log").open("wb")
        self.process = subprocess.Popen(args, stdout=self.qemu_log, stderr=self.qemu_log)
        self.wait_ready()

    def connect(self, user="ciadmin"):
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.known))
        try:
            client.connect("127.0.0.1", port=self.port, username=user, password=PASSWORD,
                           allow_agent=False, look_for_keys=False, timeout=5, banner_timeout=10, auth_timeout=10)
        except BaseException:
            client.close()
            raise
        return client

    def command(self, command, *, root=False, timeout=60):
        import shlex
        client = self.connect()
        try:
            if root:
                command = "sudo -n /bin/sh -c " + shlex.quote(command)
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out, err = stdout.read().decode(), stderr.read().decode()
            code = stdout.channel.recv_exit_status()
            if code:
                raise AssertionError(f"Ошибка управления испытательной VM: {code}: {err[:2000]}")
            return out
        finally:
            client.close()

    def wait_ready(self, old_boot=None):
        deadline = time.monotonic() + 600
        last_error = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("QEMU завершился: " + (self.root / "qemu.log").read_text(errors="replace")[-2000:])
            try:
                boot = self.command("test -e /run/relay-ci-ready || test -e /var/lib/cloud/instance/boot-finished; cat /proc/sys/kernel/random/boot_id")
                if old_boot is None or boot.strip() != old_boot:
                    self.command("test $(cat /proc/1/comm) = systemd; test -f /sys/fs/cgroup/cgroup.controllers; command -v python3 systemctl systemd-run")
                    return boot.strip()
            except (OSError, EOFError, paramiko.SSHException, AssertionError) as exc:
                last_error = type(exc).__name__
            time.sleep(2)
        raise RuntimeError(f"Ubuntu не готова за 600 секунд: {last_error}")

    def reboot(self):
        old = self.command("cat /proc/sys/kernel/random/boot_id").strip()
        self.command("systemd-run --quiet --on-active=1 /usr/bin/systemctl reboot", root=True)
        return self.wait_ready(old)

    def reset(self):
        # Аппаратный сброс только гостевой машины; имитирует внезапную перезагрузку.
        import socket
        old = self.command("cat /proc/sys/kernel/random/boot_id").strip()
        with socket.create_connection(("127.0.0.1", 44452), timeout=10) as connection:
            stream = connection.makefile("rwb")
            if "QMP" not in json.loads(stream.readline()):
                raise RuntimeError("Нет приветствия QMP")
            for operation in ("qmp_capabilities", "system_reset"):
                stream.write((json.dumps({"execute": operation}) + "\n").encode())
                stream.flush()
                while True:
                    response = json.loads(stream.readline())
                    if "error" in response:
                        raise RuntimeError("Отказ QMP")
                    if "return" in response:
                        break
        return self.wait_ready(old)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
        if hasattr(self, "qemu_log"):
            self.qemu_log.close()
