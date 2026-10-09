#  Android 2 Linux Notifications - A way to display Android phone notifications on Linux
#  Copyright (C) 2023  Patrick Zwick and contributors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import io
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from argparse import Namespace
from importlib import metadata
from pathlib import Path
from typing import Optional

import gi
import qrcode  # type: ignore
import zmq
import zmq.auth
import zmq.auth.thread
import zmq.error
from PIL import Image

gi.require_version('Notify', '0.7')

from gi.repository import Notify, GLib  # type: ignore # noqa: E402

BOLD = "\033[1m"
RESET = "\033[0m"

DEFAULT_PORT = 23045

active_notifications = []

def main() -> None:
    args = parse_args()

    if args.command == "version":
        print(f"Android 2 Linux Notifications {metadata.version('a2ln')}")
        print()
        print("For help, see <https://patri9ck.dev/a2ln/>.")

        return

    main_directory = Path(Path.home(), os.environ.get("XDG_CONFIG_HOME") or ".config", "a2ln")

    clients_directory = main_directory / "clients"
    own_directory = main_directory / "server"

    main_directory.mkdir(exist_ok=True)

    clients_directory.mkdir(exist_ok=True)

    if not own_directory.exists():
        own_directory.mkdir()

        zmq.auth.create_certificates(own_directory, "server")

    own_keys_file = own_directory / "server.key_secret"

    try:
        own_public_key, own_secret_key = zmq.auth.load_certificate(own_keys_file)
    except OSError:
        print(f"Own keys file at {own_keys_file} does not exist.")

        exit(1)
    except ValueError:
        print(f"Own keys file at {own_keys_file} is missing the public key.")

        exit(1)

    if args.command == "pair":
        server = PairingServer(clients_directory, own_public_key, args.ip, args.port)
    elif own_secret_key:
        server = NotificationServer(clients_directory, own_public_key, own_secret_key, args.ip, args.port,
                                    args.title_format, args.body_format, args.command, args.disable,
                                    args.android_ip, args.android_port)

        signal.signal(signal.SIGUSR1, lambda number, frame: server.toggle())
    else:
        print(f"Own keys file at {own_keys_file} is missing the private key.")

        exit(1)

    try:
        server.start()

        while server.is_alive():
            time.sleep(1)

        exit(1)
    except KeyboardInterrupt:
        print("\r", end="")


def parse_args() -> Namespace:
    argument_parser = argparse.ArgumentParser(description="A way to display Android phone notifications on Linux")

    argument_parser.add_argument("--android-ip", type=str, default="192.168.1.45", help="The IP of the Android device")
    argument_parser.add_argument("--android-port", type=int, default=5555, help="The port of the Android device")
    argument_parser.add_argument("--ip", type=str, default="*", help="The IP to listen")
    argument_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"The port to listen)")
    argument_parser.add_argument("--title-format", type=str, default="{title}", help="The format of the title. "
                                                                                     "Available placeholders: {app}, "
                                                                                     "{title}, {body}, {package}, {source}")
    argument_parser.add_argument("--body-format", type=str, default="{body}", help="The format of the body. Available "
                                                                                   "placeholders: {app}, {title}, "
                                                                                   "{body}, {package}, {source}")
    argument_parser.add_argument("--command", type=str, help="A shell command to run whenever a notification arrives. "
                                                             "Available placeholders: {app}, {title}, {body}, {package}")

    argument_parser.add_argument("--disable", action="store_true",
                                 help="Disables the display of notifications initially. This can be toggled during runtime using a SIGUSR1 signal.")

    sub_parser = argument_parser.add_subparsers(title="commands", dest="command")

    sub_parser.add_parser("version", help="Show the version and exit")

    pair_parser = sub_parser.add_parser("pair", help="Run the pairing server")

    pair_parser.add_argument("--ip", type=str, default="*", help="The IP to listen")
    pair_parser.add_argument("--port", type=int, help="The port to listen, random by default")

    return argument_parser.parse_args()


def get_ip() -> str:
    try:
        result = subprocess.run(
            ["ip", "route", "show", "table", "main", "default"],
            capture_output=True, text=True, check=True,
        )

        match = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", result.stdout)

        if match:
            return match.group(1)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.connect(("8.8.8.8", 80))

        return client.getsockname()[0]


def open_android_app(notification, action, package, android_ip="192.168.1.45", android_port=5555):
    print(f"CLICKED! package={package}", flush=True)

    try:
        subprocess.Popen([
            "adb",
            "-s",
            f"{android_ip}:{android_port}",
            "shell",
            "monkey",
            "-p",
            package,
            "1",
        ])

        subprocess.Popen([
            "scrcpy",
            "-s",
            f"{android_ip}:{android_port}",
        ])

    except Exception:
        traceback.print_exc()

def open_android_app(notification, action, package, android_ip="192.168.1.45", android_port=5555):
    print(f"CLICKED! package={package}", flush=True)

    try:
        # Androidの画面を起こす
        wake_result = subprocess.run(
            [
                "adb",
                "-s",
                f"{android_ip}:{android_port}",
                "shell",
                "input",
                "keyevent",
                "KEYCODE_WAKEUP",
            ],
            capture_output=True,
            text=True,
        )

        print(
            f"WAKEUP: {wake_result.returncode}",
            flush=True,
        )

        # Linux側へ通知
        subprocess.Popen([
            "notify-send",
            "Android",
            f"{package} を開きます",
        ])

        # 解除可能なキーガードなら解除を試す
        subprocess.run(
            [
                "adb",
                "-s",
                f"{android_ip}:{android_port}",
                "shell",
                "wm",
                "dismiss-keyguard",
            ],
            check=False,
        )

        # アプリ起動
        result = subprocess.run(
            [
                "adb",
                "-s",
                f"{android_ip}:{android_port}",
                "shell",
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            capture_output=True,
            text=True,
        )

        print("APP:", result.stdout.strip(), flush=True)

        # scrcpy起動
        subprocess.Popen([
            "scrcpy",
            "-s",
            f"{android_ip}:{android_port}",
        ])

    except Exception:
        traceback.print_exc()

def send_notification(
    title: str,
    body: str,
    package: str,
    picture_file=None,
    android_ip="192.168.1.45",
    android_port=5555,
) -> None:
    print(f"SENDING: {package}", flush=True)

    if picture_file is None:
        notification = Notify.Notification.new(
            title,
            body,
            "dialog-information",
        )
    else:
        notification = Notify.Notification.new(
            title,
            body,
            picture_file.name,
        )

    print("ADDING ACTION", flush=True)

    notification.add_action(
        "default",
        "開く",
        lambda n, a, p: open_android_app(n, a, p, android_ip, android_port),
        package,
    )

    # Notificationオブジェクトを生存させる
    active_notifications.append(notification)

    print("SHOWING", flush=True)
    notification.show()

    if picture_file is not None:
        picture_file.close()

def handle_error(error: zmq.error.ZMQError) -> None:
    if error.errno == zmq.EADDRINUSE:
        print("Port is already used.")
    elif error.errno == 13:
        print("Permission is missing (note that you must use a port higher than 1023 if you are not root).")
    elif error.errno == 19:
        print("IP is invalid.")
    else:
        traceback.print_exc()


class NotificationServer(threading.Thread):
    def __init__(self, clients_directory: Path, own_public_key: bytes, own_secret_key: bytes, ip: str,
                 port: int, title_format: str, body_format: str, command: Optional[str], disabled: bool,
                 android_ip: str, android_port: int):
        super(NotificationServer, self).__init__(daemon=True)

        self.clients_directory = clients_directory
        self.own_public_key = own_public_key
        self.own_secret_key = own_secret_key
        self.ip = ip
        self.port = port
        self.title_format = title_format
        self.body_format = body_format
        self.command = command
        self.disabled = disabled
        self.android_ip = android_ip
        self.android_port = android_port

    def run(self) -> None:
        super(NotificationServer, self).run()

        with zmq.Context() as context:
            authenticator = zmq.auth.thread.ThreadAuthenticator(context)

            authenticator.start()

            authenticator.configure_curve(domain="*", location=self.clients_directory.as_posix())

            with context.socket(zmq.PULL) as server:
                server.curve_publickey = self.own_public_key
                server.curve_secretkey = self.own_secret_key

                server.curve_server = True

                try:
                    server.bind(f"tcp://{self.ip}:{self.port}")
                except zmq.error.ZMQError as error:
                    authenticator.stop()

                    handle_error(error)

                    return

                print(
                    f"Notification server running on IP {BOLD}{self.ip}{RESET} and port {BOLD}{self.port}{RESET} with notifications {BOLD}{"disabled" if self.disabled else "enabled"}{RESET}.")

                Notify.init("Android 2 Linux Notifications")

                while True:
                    if server.poll(100):
                        request = server.recv_multipart(copy=False)

                        length = len(request)

                        if length != 4 and length != 5:
                            continue

                        if length == 5:
                            picture_file = tempfile.NamedTemporaryFile(suffix=".png")

                            Image.open(
                                io.BytesIO(request[4].bytes)
                            ).save(picture_file.name)
                        else:
                            picture_file = None

                        source = request[0].get("Peer-Address")
                        app = request[0].bytes.decode("utf-8")
                        title = request[1].bytes.decode("utf-8")
                        body = request[2].bytes.decode("utf-8")
                        package = request[3].bytes.decode("utf-8")

                        print()
                        print(
                            f"Received notification from {BOLD}{source}{RESET} "
                            f"(App: {BOLD}{app}{RESET}, "
                            f"Title: {BOLD}{title}{RESET}, "
                            f"Body: {BOLD}{body}{RESET}, "
                            f"Package: {BOLD}{package}{RESET})"
                        )

                        def replace(text: str) -> str:
                            return (
                                text.replace("{app}", app)
                                .replace("{title}", title)
                                .replace("{body}", body)
                                .replace("{package}", package)
                                .replace("{source}", source)
                            )

                        if not self.disabled:
                            send_notification(
                                replace(self.title_format),
                                replace(self.body_format),
                                package,
                                picture_file,
                                self.android_ip,
                                self.android_port,
                            )

                        if self.command is not None:
                            subprocess.Popen(
                                replace(self.command),
                                shell=True,
                            )

                    while GLib.MainContext.default().iteration(False):
                        pass

    def toggle(self) -> None:
        self.disabled = not self.disabled

        print()

        if self.disabled:
            print(f"Notifications {BOLD}disabled{RESET}.")
        else:
            print(f"Notifications {BOLD}enabled{RESET}.")


class PairingServer(threading.Thread):
    def __init__(self, clients_directory: Path, own_public_key: bytes, ip: str, port: Optional[int]):
        super(PairingServer, self).__init__(daemon=True)

        self.clients_directory = clients_directory
        self.own_public_key = own_public_key
        self.ip = ip
        self.port = port

    def run(self) -> None:
        super(PairingServer, self).run()

        with zmq.Context() as context, context.socket(zmq.REP) as server:
            try:
                if self.port is None:
                    self.port = server.bind_to_random_port(f"tcp://{self.ip}")
                else:
                    server.bind(f"tcp://{self.ip}:{self.port}")
            except zmq.error.ZMQError as error:
                handle_error(error)

                return

            ip = get_ip()

            qr_code = qrcode.QRCode()

            qr_code.add_data(f"{ip}:{self.port}")
            qr_code.print_ascii()

            print(f"Pairing server running on IP {BOLD}{self.ip}{RESET} and port {BOLD}{self.port}{RESET}. To pair a "
                  f"new device, open the Android 2 Linux Notifications app and scan this QR code or enter the "
                  f"following:")
            print(f"IP: {BOLD}{ip}{RESET}")
            print(f"Port: {BOLD}{self.port}{RESET}")
            print()
            print(f"Public Key: {BOLD}{self.own_public_key.decode('utf-8')}{RESET}")
            print()
            print("After pairing, ensure to restart any running notification servers.")

            while True:
                request = server.recv_multipart()

                if len(request) != 2:
                    continue

                client_ip = request[0].decode("utf-8")
                client_public_key = request[1].decode("utf-8")

                print()
                print("New pairing request:")
                print()
                print(f"IP: {BOLD}{client_ip}{RESET}")
                print(f"Public Key: {BOLD}{client_public_key}{RESET}")
                print()

                if input("Accept? (Yes/No): ").strip().lower() != "yes":
                    print("Pairing cancelled.")

                    server.send(b"")

                    continue

                with open((self.clients_directory / client_ip).as_posix() + ".key", "w",
                          encoding="utf-8") as client_file:
                    client_file.write("metadata\n"
                                      "curve\n"
                                      f"    public-key = \"{client_public_key}\"\n")

                server.send(self.own_public_key)

                print("Pairing finished.")

                if input("Pair another device? (Yes/No): ").strip().lower() != "yes":
                    break
