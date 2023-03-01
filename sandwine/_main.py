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
import shlex
import shutil
import signal
import subprocess
import sys
import time
from argparse import ArgumentParser, RawTextHelpFormatter
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, auto
from operator import attrgetter, itemgetter
from textwrap import dedent
from typing import Optional

import coloredlogs

from sandwine._metadata import DESCRIPTION, VERSION

_logger = logging.getLogger(__name__)


class AccessMode(Enum):
    READ_ONLY = 'ro'
    READ_WRITE = 'rw'


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


class MountMode(Enum):
    DEVTMPFS = auto()
    BIND_RO = auto()
    BIND_RW = auto()
    BIND_DEV = auto()
    TMPFS = auto()
    PROC = auto()


def parse_command_line(args):
    usage = dedent('''\
        usage: sandwine [OPTIONS] [--] PROGRAM [ARG ..]
           or: sandwine [OPTIONS] --configure
           or: sandwine --help
           or: sandwine --version
    ''')[len('usage: '):]

    parser = ArgumentParser(
        prog='sandwine',
        usage=usage,
        description=DESCRIPTION,
        formatter_class=RawTextHelpFormatter,
        epilog=dedent("""\
        Software libre licensed under GPL v3 or later.
        Brought to you by Sebastian Pipping <sebastian@pipping.org>.

        Please report bugs at https://github.com/hartwork/sandwine — thank you!
    """),
    )

    parser.add_argument('--version', action='version', version=VERSION)

    program = parser.add_argument_group('positional arguments')
    program.add_argument('argv_0', metavar='PROGRAM', nargs='?', help='command to run')
    program.add_argument('argv_1_plus',
                         metavar='ARG',
                         nargs='*',
                         help='arguments to pass to PROGRAM')

    x11_args = parser.add_argument_group('X11 arguments')
    x11_args.set_defaults(x11=X11Mode.NONE)
    x11_args.add_argument('--x11',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.AUTO,
                          help='enable nested X11 using X2Go nxagent or Xephry or Xnest or Xvfb'
                          ' (default: X11 disabled)')
    x11_args.add_argument('--nxagent',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.NXAGENT,
                          help='enable nested X11 using X2Go nxagent (default: X11 disabled)')
    x11_args.add_argument('--xephyr',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.XEPHYR,
                          help='enable nested X11 using Xephry (default: X11 disabled)')
    x11_args.add_argument('--xnest',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.XNEST,
                          help='enable nested X11 using Xnest (default: X11 disabled)')
    x11_args.add_argument('--xvfb',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.XVFB,
                          help='enable nested X11 using Xvfb (default: X11 disabled)')
    x11_args.add_argument('--host-x11-danger-danger',
                          dest='x11',
                          action='store_const',
                          const=X11Mode.HOST,
                          help='enable use of host X11 (CAREFUL!) (default: X11 disabled)')

    networking = parser.add_argument_group('networking arguments')
    networking.add_argument('--network',
                            action='store_true',
                            help='enable networking (default: networking disabled)')

    sound = parser.add_argument_group('sound arguments')
    sound.add_argument('--pulseaudio',
                       action='store_true',
                       help='enable sound using PulseAudio (default: sound disabled)')

    mount = parser.add_argument_group('mount arguments')
    mount.add_argument('--dotwine',
                       metavar='PATH:{ro,rw}',
                       help='use PATH for ~/.wine/ (default: use tmpfs, empty and non-persistant)')
    mount.add_argument('--pass',
                       dest='extra_binds',
                       default=[],
                       action='append',
                       metavar='PATH:{ro,rw}',
                       help='bind mount host PATH on PATH (CAREFUL!)')

    general = parser.add_argument_group('general operation arguments')
    general.add_argument('--configure',
                         action='store_true',
                         help='enforce running winecfg before start of PROGRAM'
                         ' (default: run winecfg as needed)')
    general.add_argument('--no-wine',
                         dest='with_wine',
                         default=True,
                         action='store_false',
                         help='run PROGRAM without use of Wine'
                         ' (default: run command "wine PROGRAM [ARG ..]")')
    general.add_argument('--retry',
                         dest='second_try',
                         action='store_true',
                         help='on non-zero exit code run PROGRAM a second time'
                         '; helps to workaround weird graphics-related crashes'
                         ' (default: run command once)')

    return parser.parse_args(args)


class X11Context:

    def __init__(self, config):
        self._config = config
        self._geometry = '1024x768'
        self._process = None

    @staticmethod
    def get_host_display() -> int:
        try:
            return int(os.environ['DISPLAY'][1:])
        except (KeyError, ValueError):
            return 0

    @staticmethod
    def find_unused_display() -> int:
        used_displays = {int(os.path.basename(p)[1:]) for p in glob.glob('/tmp/.X11-unix/X*')}
        candidate_displays = set(list(range(len(used_displays))) + [len(used_displays)])
        return sorted(candidate_displays - used_displays)[-1]

    @staticmethod
    def get_unix_socket_for(display: int) -> str:
        return f'/tmp/.X11-unix/X{display}'

    @staticmethod
    def detect_nested():
        tests = [
            ['nxagent', X11Mode.NXAGENT],
            ['Xephyr', X11Mode.XEPHYR],
            ['Xnest', X11Mode.XNEST],
            ['Xvfb', X11Mode.XVFB],
        ]
        for command, mode in tests:
            if shutil.which(command) is not None:
                _logger.info(f'Using {command} for nested X11.')
                return mode

        commands = [command for command, _ in tests]
        _logger.error(f'Neither {" nor ".join(commands)} is available, please install, aborting.')
        sys.exit(127)

    def __enter__(self):
        if X11Mode(self._config.x11) != X11Mode.HOST:
            # NOTE: Extension MIT-SHM is disabled because it kept crashing Xephyr 21.1.7
            #       and nxagent/X2Go 4.1.0.3 when moving windows around near screen edges.
            #       PS: Xnest does not support extension MIT-SHM.
            if X11Mode(self._config.x11) == X11Mode.XEPHYR:
                argv = ['Xephyr', '-screen', self._geometry, '-extension', 'MIT-SHM']
            elif X11Mode(self._config.x11) == X11Mode.XNEST:
                argv = ['Xnest', '-geometry', self._geometry]
            elif X11Mode(self._config.x11) == X11Mode.NXAGENT:
                argv = ['nxagent', '-nolisten', 'tcp', '-ac', '-noshmem', '-R']
            elif X11Mode(self._config.x11) == X11Mode.XVFB:
                argv = ['Xvfb', '-screen', '0', f'{self._geometry}x24', '-extension', 'MIT-SHM']
            else:
                assert False, f'X11 mode {self._config.x11} not supported'

            argv += [f':{self._config.x11_display_number}']

            _logger.info('Starting nested X11...')
            try:
                self._process = subprocess.Popen(argv)
            except FileNotFoundError:
                _logger.error(f'Command {argv[0]!r} is not available, aborting.')
                sys.exit(127)

        x11_unix_socket = self.get_unix_socket_for(self._config.x11_display_number)
        while not os.path.exists(x11_unix_socket):
            time.sleep(0.5)

    def __exit__(self, exc_type, exc_value, traceback):
        if self._process is not None:
            _logger.info('Shutting down nested X11...')
            self._process.send_signal(signal.SIGINT)
            self._process.wait()


class ArgvBuilder:

    def __init__(self):
        self._groups = []

    def add(self, *args):
        if not args:
            return
        self._groups.append(args)

    def iter_flat(self):
        for group in self._groups:
            yield from group

    def iter_groups(self):
        yield from self._groups

    def announce_to(self, target):
        for i, group in enumerate(self._groups):
            prefix = '# ' if (i == 0) else ' ' * 4
            flat_args = ' '.join(shlex.quote(arg) for arg in group)
            suffix = '' if (i == len(self._groups) - 1) else ' \\'
            print(f'{prefix}{flat_args}{suffix}', file=target)


def single_trailing_sep(path):
    return path.rstrip(os.sep) + os.sep


def parse_path_colon_access(candidate):
    error_message = f'Value {candidate!r} does not match pattern "PATH:{{ro,rw}}".'
    if ':' not in candidate:
        raise ValueError(error_message)

    path, access_mode_candidate = candidate.rsplit(':', 1)
    if access_mode_candidate == 'ro':
        return path, AccessMode.READ_ONLY
    elif access_mode_candidate == 'rw':
        return path, AccessMode.READ_WRITE

    raise ValueError(error_message)


@dataclass
class MountTask:
    mode: MountMode
    target: str
    source: Optional[str] = None
    required: bool = True


def create_bwrap_argv(config):
    my_home = os.path.realpath(os.path.expanduser('~'))
    mount_tasks = [
        MountTask(MountMode.TMPFS, '/'),
        MountTask(MountMode.BIND_RO, '/bin'),
        MountTask(MountMode.DEVTMPFS, '/dev'),
        MountTask(MountMode.BIND_DEV, '/dev/dri'),
        MountTask(MountMode.BIND_RO, '/etc'),
        MountTask(MountMode.BIND_RO, '/lib'),
        MountTask(MountMode.BIND_RO, '/lib32', required=False),
        MountTask(MountMode.BIND_RO, '/lib64'),
        MountTask(MountMode.PROC, '/proc'),
        MountTask(MountMode.BIND_RO, '/sys'),
        MountTask(MountMode.TMPFS, '/tmp'),
        MountTask(MountMode.BIND_RO, '/usr'),
        MountTask(MountMode.TMPFS, my_home),
    ]
    env_tasks = {var: None for var in ['HOME', 'TERM', 'USER', 'WINEDEBUG']}
    unshare_args = ['--unshare-all']

    argv = ArgvBuilder()

    argv.add('bwrap')
    argv.add('--new-session')

    # Networking
    if config.network:
        unshare_args += ['--share-net']
        mount_tasks += [
            MountTask(MountMode.BIND_RO, '/run/NetworkManager/resolv.conf', required=False)
        ]

    # Sound
    if config.pulseaudio:
        pulseaudio_socket = f'/run/user/{os.getuid()}/pulse/native'
        env_tasks['PULSE_SERVER'] = f'unix:{pulseaudio_socket}'
        mount_tasks += [MountTask(MountMode.BIND_RW, pulseaudio_socket)]

    # X11
    if X11Mode(config.x11) != X11Mode.NONE:
        x11_unix_socket = X11Context.get_unix_socket_for(config.x11_display_number)
        mount_tasks += [MountTask(MountMode.BIND_RW, x11_unix_socket)]
        env_tasks['DISPLAY'] = f':{config.x11_display_number}'

    # Wine
    run_winecfg = (X11Mode(config.x11) != X11Mode.NONE
                   and (config.configure or config.dotwine is None))
    dotwine_target_path = os.path.expanduser('~/.wine')
    if config.dotwine is not None:
        dotwine_source_path, dotwine_access = parse_path_colon_access(config.dotwine)

        if dotwine_access == AccessMode.READ_WRITE:
            mount_mode = MountMode.BIND_RW
        else:
            mount_mode = MountMode.BIND_RO

        mount_tasks += [MountTask(mount_mode, dotwine_target_path, source=dotwine_source_path)]

        if not os.path.exists(dotwine_source_path):
            _logger.info(f'Creating directory {dotwine_source_path!r}...')
            os.makedirs(dotwine_source_path, mode=0o700, exist_ok=True)
            run_winecfg = True

        del dotwine_source_path
        del dotwine_access
    else:
        mount_tasks += [MountTask(MountMode.TMPFS, dotwine_target_path)]
    del dotwine_target_path

    # Extra binds
    for bind in config.extra_binds:
        mount_target, mount_access = parse_path_colon_access(bind)
        if mount_access == AccessMode.READ_WRITE:
            mount_mode = MountMode.BIND_RW
        else:
            mount_mode = MountMode.BIND_RO
        mount_tasks += [MountTask(mount_mode, mount_target)]
        del mount_target, mount_access

    # Program
    if os.sep in (config.argv_0 or ''):
        real_argv_0 = os.path.realpath(config.argv_0)
        mount_tasks += [
            MountTask(MountMode.BIND_RO, real_argv_0, required=False),
            MountTask(MountMode.BIND_RO, real_argv_0 + '.exe', required=False),
            MountTask(MountMode.BIND_RO, real_argv_0 + '.EXE', required=False),
        ]

    # Linux Namespaces
    argv.add(*unshare_args)

    # Mount stack
    sorted_mount_tasks = sorted(mount_tasks, key=attrgetter('target'))
    del mount_tasks

    for mount_task in sorted_mount_tasks:
        if mount_task.mode == MountMode.TMPFS:
            argv.add('--tmpfs', mount_task.target)
        elif mount_task.mode == MountMode.DEVTMPFS:
            argv.add('--dev', mount_task.target)
        elif mount_task.mode == MountMode.PROC:
            argv.add('--proc', mount_task.target)
        elif mount_task.mode in (MountMode.BIND_RO, MountMode.BIND_RW, MountMode.BIND_DEV):
            if mount_task.source is None:
                mount_task.source = mount_task.target

            # NOTE: The X11 Unix socket will only show up later
            keep_missing_target = X11Mode(
                config.x11) != X11Mode.NONE and mount_task.target == x11_unix_socket

            if not os.path.exists(mount_task.target) and not keep_missing_target:
                if mount_task.required:
                    _logger.error(
                        f'Path {mount_task.target!r} does not exist on the host, aborting.')
                    sys.exit(1)
                else:
                    _logger.debug(f'Path {mount_task.target!r} does not exist on the host'
                                  ', dropped from mount tasks.')
                    continue

            if mount_task.mode == MountMode.BIND_RO:
                argv.add('--ro-bind', mount_task.source, mount_task.target)
            elif mount_task.mode == MountMode.BIND_RW:
                argv.add('--bind', mount_task.source, mount_task.target)
            elif mount_task.mode == MountMode.BIND_DEV:
                argv.add('--dev-bind', mount_task.source, mount_task.target)
            else:
                assert False, f'Mode {mount_task.mode} not supported'
        else:
            assert False, f'Mode {mount_task.mode} unknown'

    # Filter ${PATH}
    candidate_paths = os.environ['PATH'].split(os.pathsep)
    available_paths = []
    for candidate_path in candidate_paths:
        candidate_path = os.path.realpath(candidate_path)
        for mount_task in reversed(sorted_mount_tasks):
            if single_trailing_sep(candidate_path).startswith(single_trailing_sep(
                    mount_task.target)):
                if mount_task.mode in (MountMode.BIND_RO, MountMode.BIND_RW, MountMode.BIND_DEV):
                    available_paths.append(candidate_path)
                    break
        else:
            _logger.debug(f'Path {candidate_path!r} will not exist in sandbox mount stack'
                          ', dropped from ${PATH}.')
    env_tasks['PATH'] = os.pathsep.join(available_paths)

    # Create environment (meaning environment variables)
    argv.add('--clearenv')
    for env_var, env_value in sorted(env_tasks.items(), key=itemgetter(0)):
        if env_value is None:
            env_value = os.environ.get(env_var)
            if env_value is None:
                continue
        argv.add('--setenv', env_var, env_value)

    argv.add('--')

    # Wrap with wineserver (for clean shutdown, it defaults to 3 seconds timout)
    if config.with_wine:
        argv.add('sh', '-c', 'wineserver -p0 && "$0" "$@" ; ret=$? ; wineserver -k ; exit ${ret}')

    # Add winecfg
    if run_winecfg and config.with_wine:
        argv.add('sh', '-c', 'winecfg && exec "$0" "$@"')

    # Add second try
    if config.second_try:
        argv.add('sh', '-c', '"$0" "$@" || exec "$0" "$@"')

    # Add Wine
    if config.argv_0 is not None:
        if config.with_wine:
            argv.add('wine', config.argv_0, *config.argv_1_plus)
        else:
            argv.add(config.argv_0, *config.argv_1_plus)
    else:
        argv.add('true')

    return argv


def main():
    exit_code = 0
    try:
        config = parse_command_line(sys.argv[1:])

        coloredlogs.install(level=logging.DEBUG)

        if X11Mode(config.x11) != X11Mode.NONE:
            if X11Mode(config.x11) == X11Mode.AUTO:
                config.x11 = X11Context.detect_nested()

            if X11Mode(config.x11) == X11Mode.HOST:
                config.x11_display_number = X11Context.get_host_display()
            else:
                config.x11_display_number = X11Context.find_unused_display()

            _logger.info('Using display ":%s"...', config.x11_display_number)

            x11context = X11Context(config)
        else:
            x11context = nullcontext()

        argv_builder = create_bwrap_argv(config)
        argv_builder.announce_to(sys.stderr)

        argv = list(argv_builder.iter_flat())

        with x11context:
            try:
                exit_code = subprocess.call(argv)
            except FileNotFoundError:
                _logger.error(f'Command {argv[0]!r} is not available, aborting.')
                exit_code = 127

    except KeyboardInterrupt:
        exit_code = 128 + signal.SIGINT

    sys.exit(exit_code)
