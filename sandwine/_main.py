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

import logging
import os
import random
import shlex
import signal
import subprocess
import sys
from argparse import ArgumentParser, RawTextHelpFormatter
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, auto
from importlib.metadata import metadata
from operator import attrgetter, itemgetter
from textwrap import dedent
from typing import Optional

import coloredlogs

from sandwine._x11 import X11Display, X11Mode, create_x11_context, detect_and_require_nested_x11

_logger = logging.getLogger(__name__)


class AccessMode(Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


class MountMode(Enum):
    BIND_DEV = auto()
    BIND_RO = auto()
    BIND_RW = auto()
    DEVTMPFS = auto()
    PROC = auto()
    SYMLINK = auto()
    TMPFS = auto()


def parse_command_line(args: list[str], with_wine: bool):
    distribution = metadata("sandwine")

    prog = "sandwine" if with_wine else "sand"
    description = (
        distribution["Summary"]
        if with_wine
        else "Command-line tool to run commands with bwrap/bubblewrap isolation"
    )

    usage = dedent(f"""\
        usage: {prog} [OPTIONS] [--] PROGRAM [ARG ..]
           or: {prog} [OPTIONS] --configure
           or: {prog} --help
           or: {prog} --version
    """)[len("usage: ") :]

    parser = ArgumentParser(
        prog=prog,
        usage=usage,
        description=description,
        formatter_class=RawTextHelpFormatter,
        epilog=dedent("""\
            Software libre licensed under GPL v3 or later.
            Brought to you by Sebastian Pipping <sebastian@pipping.org>.

            Please report bugs at https://github.com/hartwork/sandwine — thank you!
        """),
    )

    parser.add_argument("--version", action="version", version=distribution["Version"])

    program = parser.add_argument_group("positional arguments")
    program.add_argument("argv_0", metavar="PROGRAM", nargs="?", help="command to run")
    program.add_argument(
        "argv_1_plus", metavar="ARG", nargs="*", help="arguments to pass to PROGRAM"
    )

    x11_args = parser.add_argument_group("X11 arguments")
    x11_args.set_defaults(x11=X11Mode.NONE)
    x11_args.add_argument(
        "--x11",
        dest="x11",
        action="store_const",
        const=X11Mode.AUTO,
        help="enable nested X11 using X2Go nxagent or Xephyr or Xnest"
        " but not Xvfb and not Xpra"
        " (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--nxagent",
        dest="x11",
        action="store_const",
        const=X11Mode.NXAGENT,
        help="enable nested X11 using X2Go nxagent (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xephyr",
        dest="x11",
        action="store_const",
        const=X11Mode.XEPHYR,
        help="enable nested X11 using Xephyr (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xnest",
        dest="x11",
        action="store_const",
        const=X11Mode.XNEST,
        help="enable nested X11 using Xnest (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xpra",
        dest="x11",
        action="store_const",
        const=X11Mode.XPRA,
        help="enable nested X11 using Xpra (EXPERIMENTAL, CAREFUL!)" " (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xvfb",
        dest="x11",
        action="store_const",
        const=X11Mode.XVFB,
        help="enable nested X11 using Xvfb (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--host-x11-danger-danger",
        dest="x11",
        action="store_const",
        const=X11Mode.HOST,
        help="enable use of host X11 (CAREFUL!) (default: X11 disabled)",
    )

    networking = parser.add_argument_group("networking arguments")
    networking.add_argument(
        "--network", action="store_true", help="enable networking (default: networking disabled)"
    )

    sound = parser.add_argument_group("sound arguments")
    sound.add_argument(
        "--pulseaudio",
        action="store_true",
        help="enable sound using PulseAudio (default: sound disabled)",
    )

    mount = parser.add_argument_group("mount arguments")
    if with_wine:
        mount.add_argument(
            "--dotwine",
            metavar="PATH:{ro,rw}",
            help="use PATH for ~/.wine/ (default: use tmpfs, empty and non-persistent)",
        )
    else:
        mount.set_defaults(dotwine=None)
    mount.add_argument(
        "--pass",
        dest="extra_binds",
        default=[],
        action="append",
        metavar="PATH:{ro,rw}",
        help="bind mount host PATH on PATH (CAREFUL!)",
    )

    general = parser.add_argument_group("general operation arguments")
    if with_wine:
        general.add_argument(
            "--configure",
            action="store_true",
            help="enforce running winecfg before start of PROGRAM"
            " (default: run winecfg as needed)",
        )
    else:
        mount.set_defaults(configure=None)
    general.add_argument(
        "--no-pty",
        dest="with_pty",
        default=True,
        action="store_false",
        help="refrain from creating a pseudo-terminal"
        ", stop protecting against TIOCSTI/TIOCLINUX hijacking (CAREFUL!)"
        " (default: create a pseudo-terminal)",
    )
    if with_wine:
        general.add_argument(
            "--no-wine",
            dest="with_wine",
            default=True,
            action="store_false",
            help="run PROGRAM without use of Wine"
            ' (default: run command "wine PROGRAM [ARG ..]")',
        )
    else:
        mount.set_defaults(with_wine=False)
    general.add_argument(
        "--retry",
        dest="second_try",
        action="store_true",
        help="on non-zero exit code run PROGRAM a second time"
        "; helps to workaround weird graphics-related crashes"
        " (default: run command once)",
    )

    return parser.parse_args(args)


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
            prefix = "# " if (i == 0) else " " * 4
            flat_args = shlex.join(group)
            suffix = "" if (i == len(self._groups) - 1) else " \\"
            print(f"{prefix}{flat_args}{suffix}", file=target)


def single_trailing_sep(path):
    return path.rstrip(os.sep) + os.sep


def parse_path_colon_access(candidate):
    error_message = f'Value {candidate!r} does not match pattern "PATH:{{ro,rw}}".'
    if ":" not in candidate:
        raise ValueError(error_message)

    path, access_mode_candidate = candidate.rsplit(":", 1)
    if access_mode_candidate == "ro":
        return path, AccessMode.READ_ONLY
    elif access_mode_candidate == "rw":
        return path, AccessMode.READ_WRITE

    raise ValueError(error_message)


@dataclass
class MountTask:
    mode: MountMode
    target: str
    source: Optional[str] = None
    required: bool = True


def infer_mount_task(mode: MountMode, abs_target_path: str, required: bool = True) -> MountTask:
    if os.path.islink(abs_target_path):
        mode = MountMode.SYMLINK
        source = os.readlink(abs_target_path)
    else:
        source = None

    return MountTask(mode=mode, target=abs_target_path, source=source, required=required)


def random_hostname():
    return "".join(hex(random.randint(0, 15))[2:] for _ in range(12))


def create_bwrap_argv(config):
    my_home = os.path.expanduser("~")
    mount_tasks = [
        MountTask(MountMode.TMPFS, "/"),
        MountTask(MountMode.BIND_RO, "/bin"),
        MountTask(MountMode.DEVTMPFS, "/dev"),
        MountTask(MountMode.BIND_DEV, "/dev/dri"),
        MountTask(MountMode.BIND_RO, "/etc"),
        infer_mount_task(MountMode.BIND_RO, "/lib"),
        infer_mount_task(MountMode.BIND_RO, "/lib32", required=False),
        infer_mount_task(MountMode.BIND_RO, "/lib64"),
        MountTask(MountMode.PROC, "/proc"),
        MountTask(MountMode.BIND_RO, "/sys"),
        MountTask(MountMode.TMPFS, "/tmp"),
        MountTask(MountMode.BIND_RO, "/usr"),
        MountTask(MountMode.TMPFS, my_home),
    ]
    env_tasks = {var: None for var in ["HOME", "TERM", "USER", "WINEDEBUG"]}
    env_tasks["container"] = "sandwine"
    unshare_args = ["--unshare-user", "--unshare-all"]

    argv = ArgvBuilder()

    argv.add("bwrap")
    argv.add("--disable-userns")
    argv.add("--die-with-parent")

    # Hostname
    hostname = random_hostname()
    env_tasks["HOSTNAME"] = hostname
    argv.add("--hostname", hostname)

    # Networking
    if config.network:
        unshare_args += ["--share-net"]
        mount_tasks += [
            MountTask(MountMode.BIND_RO, "/run/NetworkManager/resolv.conf", required=False),
            MountTask(MountMode.BIND_RO, "/run/systemd/resolve/stub-resolv.conf", required=False),
        ]

    # Sound
    if config.pulseaudio:
        pulseaudio_socket = f"/run/user/{os.getuid()}/pulse/native"
        env_tasks["PULSE_SERVER"] = f"unix:{pulseaudio_socket}"
        mount_tasks += [MountTask(MountMode.BIND_RW, pulseaudio_socket)]

    # X11
    if X11Mode(config.x11) != X11Mode.NONE:
        x11_unix_socket = X11Display(config.x11_display_number).get_unix_socket()
        mount_tasks += [MountTask(MountMode.BIND_RW, x11_unix_socket)]
        env_tasks["DISPLAY"] = f":{config.x11_display_number}"

    # Wine
    run_winecfg = X11Mode(config.x11) != X11Mode.NONE and (
        config.configure or config.dotwine is None
    )
    dotwine_target_path = os.path.expanduser("~/.wine")
    if config.dotwine is not None:
        dotwine_source_path, dotwine_access = parse_path_colon_access(config.dotwine)

        if dotwine_access == AccessMode.READ_WRITE:
            mount_mode = MountMode.BIND_RW
        else:
            mount_mode = MountMode.BIND_RO

        mount_tasks += [MountTask(mount_mode, dotwine_target_path, source=dotwine_source_path)]

        if not os.path.exists(dotwine_source_path):
            _logger.info(f"Creating directory {dotwine_source_path!r}...")
            os.makedirs(dotwine_source_path, mode=0o700, exist_ok=True)
            run_winecfg = True

        del dotwine_source_path
        del dotwine_access
    elif config.with_wine:
        mount_tasks += [MountTask(MountMode.TMPFS, dotwine_target_path)]
    del dotwine_target_path

    # Extra binds
    for bind in config.extra_binds:
        mount_target_orig, mount_access = parse_path_colon_access(bind)
        mount_target = os.path.abspath(mount_target_orig)
        del mount_target_orig
        if mount_access == AccessMode.READ_WRITE:
            mount_mode = MountMode.BIND_RW
        else:
            mount_mode = MountMode.BIND_RO
        mount_tasks += [MountTask(mount_mode, mount_target)]
        del mount_target, mount_access

    # Program
    if os.sep in (config.argv_0 or ""):
        real_argv_0 = os.path.abspath(config.argv_0)
        mount_tasks += [
            MountTask(MountMode.BIND_RO, real_argv_0, required=False),
        ]
        if config.with_wine:
            mount_tasks += [
                MountTask(MountMode.BIND_RO, real_argv_0 + ".exe", required=False),
                MountTask(MountMode.BIND_RO, real_argv_0 + ".EXE", required=False),
            ]

    # Linux Namespaces
    argv.add(*unshare_args)

    # Mount stack
    sorted_mount_tasks = sorted(mount_tasks, key=attrgetter("target"))
    del mount_tasks

    for mount_task in sorted_mount_tasks:
        if mount_task.mode == MountMode.TMPFS:
            argv.add("--tmpfs", mount_task.target)
        elif mount_task.mode == MountMode.DEVTMPFS:
            argv.add("--dev", mount_task.target)
        elif mount_task.mode == MountMode.PROC:
            argv.add("--proc", mount_task.target)
        elif mount_task.mode in (
            MountMode.BIND_RO,
            MountMode.BIND_RW,
            MountMode.BIND_DEV,
            MountMode.SYMLINK,
        ):
            if mount_task.source is None:
                mount_task.source = mount_task.target

            # NOTE: The X11 Unix socket will only show up later
            keep_missing_source = (
                X11Mode(config.x11) != X11Mode.NONE and mount_task.target == x11_unix_socket
            )

            if (
                mount_task.mode != MountMode.SYMLINK
                and not os.path.exists(mount_task.source)
                and not keep_missing_source
            ):
                if mount_task.required:
                    _logger.error(
                        f"Path {mount_task.source!r} does not exist on the host, aborting."
                    )
                    sys.exit(1)
                else:
                    _logger.debug(
                        f"Path {mount_task.source!r} does not exist on the host"
                        ", dropped from mount tasks."
                    )
                    continue

            if mount_task.mode == MountMode.BIND_RO:
                argv.add("--ro-bind", mount_task.source, mount_task.target)
            elif mount_task.mode == MountMode.BIND_RW:
                argv.add("--bind", mount_task.source, mount_task.target)
            elif mount_task.mode == MountMode.BIND_DEV:
                argv.add("--dev-bind", mount_task.source, mount_task.target)
            elif mount_task.mode == MountMode.SYMLINK:
                argv.add("--symlink", mount_task.source, mount_task.target)
            else:
                assert False, f"Mode {mount_task.mode} not supported"
        else:
            assert False, f"Mode {mount_task.mode} unknown"

    # Filter ${PATH}
    candidate_paths = os.environ["PATH"].split(os.pathsep)
    candidate_paths.append("/usr/lib/wine")  # for wineserver on e.g. Debian
    available_paths = []
    for candidate_path in candidate_paths:
        candidate_path = os.path.realpath(candidate_path)
        for mount_task in reversed(sorted_mount_tasks):
            if single_trailing_sep(candidate_path).startswith(
                single_trailing_sep(mount_task.target)
            ):
                if mount_task.mode in (MountMode.BIND_RO, MountMode.BIND_RW, MountMode.BIND_DEV):
                    available_paths.append(candidate_path)
                    break
        else:
            _logger.debug(
                f"Path {candidate_path!r} will not exist in sandbox mount stack"
                ", dropped from ${PATH}."
            )
    env_tasks["PATH"] = os.pathsep.join(available_paths)

    # Create environment (meaning environment variables)
    argv.add("--clearenv")
    for env_var, env_value in sorted(env_tasks.items(), key=itemgetter(0)):
        if env_value is None:
            env_value = os.environ.get(env_var)
            if env_value is None:
                continue
        argv.add("--setenv", env_var, env_value)

    argv.add("--")

    # Wrap with wineserver (for clean shutdown, it defaults to 3 seconds timeout)
    if config.with_wine:
        argv.add("sh", "-c", 'wineserver -p0 && "$0" "$@" ; ret=$? ; wineserver -k ; exit ${ret}')

    # Add winecfg
    if run_winecfg and config.with_wine:
        argv.add("sh", "-c", 'winecfg && exec "$0" "$@"')

    # Add second try
    if config.second_try:
        argv.add("sh", "-c", '"$0" "$@" || exec "$0" "$@"')

    # Add Wine and PTY
    if config.argv_0 is not None:
        # Add Wine
        inner_argv = []
        if config.with_wine:
            inner_argv.append("wine")
        inner_argv.append(config.argv_0)
        inner_argv.extend(config.argv_1_plus)

        # Add PTY
        if config.with_pty:
            # NOTE: This implementation is known to not support Ctrl+Z (SIGTSTP).
            #       Implementing something with Ctrl+Z support is complex and planned for later.
            #       The current approach is inspired by ptysolate by Jakub Wilk:
            #       https://github.com/jwilk/ptysolate
            argv.add("script", "-e", "-q", "-c", f"exec {shlex.join(inner_argv)}", "/dev/null")
        else:
            argv.add(*inner_argv)
    else:
        argv.add("true")

    return argv


def require_recent_bubblewrap():
    argv = ["bwrap", "--disable-userns", "--help"]
    if subprocess.call(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        _logger.error("sandwine requires bubblewrap >=0.8.0" ", aborting.")
        sys.exit(1)


def _inner_main(with_wine: bool):
    exit_code = 0
    try:
        config = parse_command_line(sys.argv[1:], with_wine=with_wine)

        coloredlogs.install(level=logging.DEBUG)

        require_recent_bubblewrap()

        if X11Mode(config.x11) != X11Mode.NONE:
            if X11Mode(config.x11) == X11Mode.AUTO:
                config.x11 = detect_and_require_nested_x11()

            if X11Mode(config.x11) == X11Mode.HOST:
                config.x11_display_number = X11Display.find_used()
            else:
                minimum = 0
                if X11Mode(config.x11) == X11Mode.XPRA:
                    minimum = 10  # Avoids warning from Xpra for displays <=9
                config.x11_display_number = X11Display.find_unused(minimum)

            _logger.info('Using display ":%s"...', config.x11_display_number)

            x11context = create_x11_context(config.x11, config.x11_display_number, 1024, 768)
        else:
            x11context = nullcontext()

        argv_builder = create_bwrap_argv(config)
        argv_builder.announce_to(sys.stderr)

        argv = list(argv_builder.iter_flat())

        with x11context:
            try:
                exit_code = subprocess.call(argv)
            except FileNotFoundError:
                _logger.error(f"Command {argv[0]!r} is not available, aborting.")
                exit_code = 127

    except KeyboardInterrupt:
        exit_code = 128 + signal.SIGINT

    sys.exit(exit_code)


def main_sand():
    _inner_main(with_wine=False)


def main_sandwine():
    _inner_main(with_wine=True)
