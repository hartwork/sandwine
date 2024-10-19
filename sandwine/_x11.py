# This file is part of the sandwine project.
#
# Copyright (c) 2023 Sebastian Pipping <sebastian@pipping.org>
#
# sandwine is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# sandwine is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with sandwine. If not, see <https://www.gnu.org/licenses/>.

import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import nullcontext
from enum import Enum
from textwrap import dedent

_logger = logging.getLogger(__name__)


def _wait_until_file_present(filename):
    while not os.path.exists(filename):
        time.sleep(0.5)


class X11Mode(Enum):
    AUTO = "auto"
    HOST = "host"
    NXAGENT = "nxagent"
    XEPHYR = "xephyr"
    XNEST = "xnest"
    XPRA = "xpra"
    XVFB = "xvfb"
    NONE = "none"

    @staticmethod
    def values():
        return [x.value for x in X11Mode.__members__.values()]


class X11Display:
    def __init__(self, display_number: int):
        self._display_number: int = display_number

    def get_unix_socket(self) -> str:
        return f"/tmp/.X11-unix/X{self._display_number}"

    def wait_until_available(self):
        x11_unix_socket = self.get_unix_socket()
        _wait_until_file_present(x11_unix_socket)

    @staticmethod
    def find_unused(minimum: int = 0) -> int:
        used_displays = {int(os.path.basename(p)[1:]) for p in glob.glob("/tmp/.X11-unix/X*")}
        candidate_displays = set(list(range(len(used_displays))) + [len(used_displays)] + [minimum])
        return sorted(candidate_displays - used_displays)[-1]

    @staticmethod
    def find_used() -> int:
        try:
            return int(os.environ["DISPLAY"][1:])
        except (KeyError, ValueError):
            return 0


class _X11Context(ABC):
    _message_starting = "Starting nested X11..."
    _message_started = "Nested X11 ready."
    _message_stopping = "Shutting down nested X11..."
    _message_stopped = "Nested X11 gone."

    _command = None

    def __init__(self, display_number: int, width: int, height: int):
        self._display_number: int = display_number
        self._geometry: str = f"{width}x{height}"

    @classmethod
    def is_available(cls):
        return shutil.which(cls._command) is not None

    @abstractmethod
    def __enter__(self):
        raise NotImplementedError

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        raise NotImplementedError


class _SimpleX11Context(_X11Context):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._process = None

    def __enter__(self):
        _logger.info(self._message_starting)

        argv = self._create_argv()
        try:
            self._process = subprocess.Popen(argv)
        except FileNotFoundError:
            _logger.error(f"Command {argv[0]!r} is not available, aborting.")
            sys.exit(127)

        X11Display(self._display_number).wait_until_available()

        _logger.info(self._message_started)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._process is not None:
            _logger.info(self._message_stopping)
            self._process.send_signal(signal.SIGINT)
            self._process.wait()
            _logger.info(self._message_stopped)


class NxagentContext(_SimpleX11Context):
    _command = "nxagent"

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command,
            "-nolisten",
            "tcp",
            "-ac",
            "-noshmem",
            "-R",
            f":{self._display_number}",
        ]


class XephyrX11Context(_SimpleX11Context):
    _command = "Xephyr"

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command,
            "-screen",
            self._geometry,
            "-extension",
            "MIT-SHM",
            f":{self._display_number}",
        ]


class XnestX11Context(_SimpleX11Context):
    _command = "Xnest"

    def _create_argv(self):
        return [self._command, "-geometry", self._geometry, f":{self._display_number}"]


class XpraContext(_X11Context):
    _command = "xpra"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client_process = None
        self._server_process = None
        self._tempdir = None

    @staticmethod
    def _write_xvfh_wrapper_script_to(xvfb_wrapper_path):
        with open(xvfb_wrapper_path, "w") as f:
            print(
                dedent("""\
                #! /usr/bin/env bash
                set -e
                args=(
                    +extension GLX
                    +extension Composite
                    # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
                    #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
                    -extension MIT-SHM
                    # NOTE: This is the Xpra default, 1024x768x24+32 was rejected in practice.
                    -screen 0 8192x4096x24+32
                    -nolisten tcp
                    -noreset
                    # NOTE: We are trying to protect the host from the app,
                    #       *not* the app from the host.
                    # -auth [..]
                    -dpi 96
                    "$@"
                )
                PS4='# '
                set -x
                exec Xvfb "${args[@]}"
            """),
                file=f,
            )
            os.fchmod(f.fileno(), 0o755)  # i.e. make executable

    def _wait_for_connectable_xpra_server(self, unix_socket_path: str) -> None:
        while True:
            ret = subprocess.call(
                [self._command, "id", unix_socket_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ret == 0:
                return
            time.sleep(0.5)

    def __enter__(self):
        _logger.info(self._message_starting)

        self._tempdir = tempfile.TemporaryDirectory()
        self._tempdir.__enter__()

        unix_socket_path = os.path.join(self._tempdir.name, "xpra-socket")
        xvfb_wrapper_path = os.path.join(self._tempdir.name, "xpra-xvfb.sh")
        sessions_path = os.path.join(self._tempdir.name, "xpra-sessions")

        self._write_xvfh_wrapper_script_to(xvfb_wrapper_path)

        server_argv = [
            self._command,
            "start",
            # NOTE: This is experimental and some of these options
            #       may need a closer look and/or re-evaluation.
            #       Experimental implies risky to depend on for security!
            "--attach=no",
            "--bandwidth-limit=0",
            "--bell=no",
            f"--bind={unix_socket_path}",
            "--clipboard=no",
            "--daemon=no",
            "--dbus-launch=",
            "--dbus-proxy=no",
            "--file-transfer=no",
            "--html=off",
            "--http-scripts=off",
            "--microphone=off",
            "--min-quality=100",
            "--open-files=no",
            "--open-url=no",
            "--printing=no",
            "--proxy-start-sessions=no",
            "--pulseaudio=no",
            "--quality=100",
            f"--sessions-dir={sessions_path}",
            "--speaker=off",
            "--start-new-commands=no",
            "--systemd-run=no",
            "--use-display=no",
            "--video-scaling=0",
            "--webcam=no",
            "--xsettings=no",
            f"--xvfb={xvfb_wrapper_path}",
            f":{self._display_number}",
        ]
        client_argv = [
            self._command,
            "attach",
            f"--sessions-dir={sessions_path}",
            unix_socket_path,
        ]

        client_env = os.environ.copy()
        client_env.pop("SSH_AUTH_SOCK", None)

        try:
            self._server_process = subprocess.Popen(server_argv)
            _wait_until_file_present(unix_socket_path)
            self._wait_for_connectable_xpra_server(unix_socket_path)
            self._client_process = subprocess.Popen(client_argv, env=client_env)
        except FileNotFoundError:
            _logger.error(f"Command {self._command!r} is not available, aborting.")
            sys.exit(127)

        X11Display(self._display_number).wait_until_available()

        _logger.info(self._message_started)

    def __exit__(self, exc_type, exc_val, exc_tb):
        _logger.info(self._message_stopping)

        for process in (self._client_process, self._server_process):
            if process is None:
                continue
            # NOTE: Using SIGTERM only because SIGINT showed backtrace output
            process.send_signal(signal.SIGTERM)
            process.wait()

        self._tempdir.__exit__(None, None, None)
        self._tempdir = None

        _logger.info(self._message_stopped)


class XvfbX11Context(_SimpleX11Context):
    _command = "Xvfb"

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command,
            "-screen",
            "0",
            f"{self._geometry}x24",
            "-extension",
            "MIT-SHM",
            f":{self._display_number}",
        ]


def detect_and_require_nested_x11() -> X11Mode:
    tests = [
        # NOTE: No Xvfb here because this is meant to be about
        #       options with user-visible Windows
        [NxagentContext, X11Mode.NXAGENT],
        [XephyrX11Context, X11Mode.XEPHYR],
        [XnestX11Context, X11Mode.XNEST],
    ]

    for clazz, mode in tests:
        if clazz.is_available():
            _logger.info(f"Using {clazz._command} for nested X11.")
            return mode

    commands = [clazz._command for clazz, _ in tests]
    _logger.error(f'Neither {" nor ".join(commands)} is available, please install, aborting.')
    sys.exit(127)


def create_x11_context(mode: X11Mode, display_number: int, width: int, height: int):
    init_args = dict(display_number=display_number, width=width, height=height)

    if mode == X11Mode.HOST:
        return nullcontext()
    elif mode == X11Mode.NXAGENT:
        return NxagentContext(**init_args)
    elif mode == X11Mode.XEPHYR:
        return XephyrX11Context(**init_args)
    elif mode == X11Mode.XNEST:
        return XnestX11Context(**init_args)
    elif mode == X11Mode.XPRA:
        return XpraContext(**init_args)
    elif mode == X11Mode.XVFB:
        return XvfbX11Context(**init_args)

    assert False, f"X11 mode {mode} not supported"
