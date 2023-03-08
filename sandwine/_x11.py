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
import time
from abc import ABC, abstractmethod
from contextlib import nullcontext
from enum import Enum

_logger = logging.getLogger(__name__)


class X11Mode(Enum):
    AUTO = 'auto'
    HOST = 'host'
    NXAGENT = 'nxagent'
    XEPHYR = 'xephyr'
    XNEST = 'xnest'
    XVFB = 'xvfb'
    NONE = 'none'

    @staticmethod
    def values():
        return [x.value for x in X11Mode.__members__.values()]


class X11Display:

    def __init__(self, display_number: int):
        self._display_number: int = display_number

    def get_unix_socket(self) -> str:
        return f'/tmp/.X11-unix/X{self._display_number}'

    def wait_until_available(self):
        x11_unix_socket = self.get_unix_socket()
        while not os.path.exists(x11_unix_socket):
            time.sleep(0.5)

    @staticmethod
    def find_unused() -> int:
        used_displays = {int(os.path.basename(p)[1:]) for p in glob.glob('/tmp/.X11-unix/X*')}
        candidate_displays = set(list(range(len(used_displays))) + [len(used_displays)])
        return sorted(candidate_displays - used_displays)[-1]

    @staticmethod
    def find_used() -> int:
        try:
            return int(os.environ['DISPLAY'][1:])
        except (KeyError, ValueError):
            return 0


class _X11Context(ABC):

    def __init__(self, display_number: int, width: int, height: int):
        self._display_number: int = display_number
        self._geometry: str = f'{width}x{height}'

    @classmethod
    @abstractmethod
    def is_available(cls):
        raise NotImplementedError

    @abstractmethod
    def __enter__(self):
        raise NotImplementedError

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        raise NotImplementedError


class _SimpleX11Context(_X11Context):
    _command = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._process = None

    @classmethod
    def is_available(cls):
        return shutil.which(cls._command) is not None

    def __enter__(self):
        _logger.info('Starting nested X11...')
        argv = self._create_argv()
        try:
            self._process = subprocess.Popen(argv)
        except FileNotFoundError:
            _logger.error(f'Command {argv[0]!r} is not available, aborting.')
            sys.exit(127)

        X11Display(self._display_number).wait_until_available()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._process is not None:
            _logger.info('Shutting down nested X11...')
            self._process.send_signal(signal.SIGINT)
            self._process.wait()


class NxagentContext(_SimpleX11Context):
    _command = 'nxagent'

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command, '-nolisten', 'tcp', '-ac', '-noshmem', '-R', f':{self._display_number}'
        ]


class XephyrX11Context(_SimpleX11Context):
    _command = 'Xephyr'

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command, '-screen', self._geometry, '-extension', 'MIT-SHM',
            f':{self._display_number}'
        ]


class XnestX11Context(_SimpleX11Context):
    _command = 'Xnest'

    def _create_argv(self):
        return [self._command, '-geometry', self._geometry, f':{self._display_number}']


class XvfbX11Context(_SimpleX11Context):
    _command = 'Xvfb'

    def _create_argv(self):
        # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
        #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
        return [
            self._command, '-screen', '0', f'{self._geometry}x24', '-extension', 'MIT-SHM',
            f':{self._display_number}'
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
            _logger.info(f'Using {clazz._command} for nested X11.')
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
    elif mode == X11Mode.XVFB:
        return XvfbX11Context(**init_args)

    assert False, f'X11 mode {mode} not supported'
