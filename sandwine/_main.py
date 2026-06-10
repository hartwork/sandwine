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
import shutil
import signal
import subprocess
import sys
import sysconfig
from argparse import ArgumentParser, RawTextHelpFormatter
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, auto
from importlib.metadata import metadata
from operator import attrgetter, itemgetter
from textwrap import dedent

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


class CommandNotFound(FileNotFoundError):
    def __init__(self, command: str):
        self._command = command

    def __str__(self):
        return f"Command {self._command!r} is not available."


class WineprefixSharingPrevented(Exception):
    pass


class UppercaseUsageRawHelpFormatter(RawTextHelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        if prefix is None:
            prefix = "Usage: "  # Note the uppercase here
        return super()._format_usage(usage, actions, groups, prefix)


def parse_command_line(args: list[str], with_wine: bool):
    distribution = metadata("sandwine")

    prog = "sandwine" if with_wine else "sand"
    description = (
        distribution["Summary"]
        if with_wine
        else "Command-line tool to run commands with bwrap/bubblewrap isolation"
    )

    usage = dedent(f"""\
        Usage: {prog} [OPTIONS] [--] PROGRAM [ARG ..]
           or: {prog} [OPTIONS] --configure
           or: {prog} --help
           or: {prog} --version
    """)[len("Usage: ") :]

    parser = ArgumentParser(
        prog=prog,
        usage=usage,
        description=description,
        formatter_class=UppercaseUsageRawHelpFormatter,
        epilog=dedent("""\
            Software libre licensed under GPL v3 or later.
            Brought to you by Sebastian Pipping <sebastian@pipping.org>.

            Please report bugs at https://github.com/hartwork/sandwine — thank you!
        """),
        add_help=False,
    )

    parser._optionals.title = parser._optionals.title.title()

    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")
    parser.add_argument(
        "--version",
        action="version",
        version=distribution["Version"],
        help="Show program's version number and exit",
    )

    program = parser.add_argument_group("Positional arguments")
    program.add_argument("argv_0", metavar="PROGRAM", nargs="?", help="Command to run")
    program.add_argument(
        "argv_1_plus", metavar="ARG", nargs="*", help="Arguments to pass to PROGRAM"
    )

    wayland_args = parser.add_argument_group("Wayland arguments")
    wayland_args.add_argument(
        "--wayland",
        action="store_true",
        help="Enable use of Wayland (default: Wayland disabled)",
    )

    x11_args = parser.add_argument_group("X11 arguments")
    x11_args.set_defaults(x11=X11Mode.NONE)
    x11_args.add_argument(
        "--x11",
        dest="x11",
        action="store_const",
        const=X11Mode.AUTO,
        help="Enable nested X11 using X2Go nxagent or Xephyr or Xnest"
        " but not Xvfb and not Xpra"
        " (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--nxagent",
        dest="x11",
        action="store_const",
        const=X11Mode.NXAGENT,
        help="Enable nested X11 using X2Go nxagent (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xephyr",
        dest="x11",
        action="store_const",
        const=X11Mode.XEPHYR,
        help="Enable nested X11 using Xephyr (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xnest",
        dest="x11",
        action="store_const",
        const=X11Mode.XNEST,
        help="Enable nested X11 using Xnest (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xpra",
        dest="x11",
        action="store_const",
        const=X11Mode.XPRA,
        help="Enable nested X11 using Xpra (EXPERIMENTAL, CAREFUL!) (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--xvfb",
        dest="x11",
        action="store_const",
        const=X11Mode.XVFB,
        help="Enable nested X11 using Xvfb (default: X11 disabled)",
    )
    x11_args.add_argument(
        "--host-x11-danger-danger",
        dest="x11",
        action="store_const",
        const=X11Mode.HOST,
        help="Enable use of host X11 (CAREFUL!) (default: X11 disabled)",
    )

    nvidia_gpu = parser.add_argument_group("GPU arguments")
    nvidia_gpu.add_argument(
        "--nvidia-gpu",
        action="store_true",
        help="Enable Nvidia GPU access (default: Nvidia GPU access disabled)",
    )

    networking = parser.add_argument_group("Networking arguments")
    networking.add_argument(
        "--network", action="store_true", help="Enable networking (default: networking disabled)"
    )

    sound = parser.add_argument_group("Sound arguments")
    sound.add_argument(
        "--pulseaudio",
        action="store_true",
        help="Enable sound using PulseAudio (default: sound disabled)",
    )
    sound.add_argument(
        "--pipewire",
        action="store_true",
        help="Enable sound using PipeWire (default: sound disabled)",
    )

    input_args = parser.add_argument_group("Input arguments")
    input_args.add_argument(
        "--raw-input",
        dest="raw_input",
        action="store_true",
        help="Enable access to /dev/input for gamepads (CAREFUL!) (default: raw input disabled)",
    )

    mount = parser.add_argument_group("Mount arguments")
    if with_wine:
        mount.add_argument(
            "--dotwine",
            metavar="PATH:{ro,rw}",
            help="Use PATH for ~/.wine/ (default: use tmpfs, empty and non-persistent)",
            type=parse_path_colon_access,
        )
    else:
        mount.set_defaults(dotwine=None)
    mount.add_argument(
        "--pass",
        dest="extra_binds",
        default=[],
        action="append",
        metavar="PATH:{ro,rw}",
        help="Bind mount host PATH on PATH (CAREFUL!)",
    )

    general = parser.add_argument_group("General operation arguments")
    if with_wine:
        general.add_argument(
            "--configure",
            action="store_true",
            help="Enforce running winecfg before start of PROGRAM (default: run winecfg as needed)",
        )
    else:
        mount.set_defaults(configure=None)
    general.add_argument(
        "--no-pty",
        dest="with_pty",
        default=True,
        action="store_false",
        help="Refrain from creating a pseudo-terminal"
        ", stop protecting against TIOCSTI/TIOCLINUX hijacking (CAREFUL!)"
        " (default: create a pseudo-terminal)",
    )
    if with_wine:
        general.add_argument(
            "--no-wine",
            dest="with_wine",
            default=True,
            action="store_false",
            help='Run PROGRAM without use of Wine (default: run command "wine PROGRAM [ARG ..]")',
        )
    else:
        mount.set_defaults(with_wine=False)
    general.add_argument(
        "--retry",
        dest="second_try",
        action="store_true",
        help="On non-zero exit code run PROGRAM a second time"
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


def _resolve_executable_file(path: str) -> str | None:
    resolved = os.path.realpath(path)
    if os.access(resolved, os.X_OK) and os.path.isfile(resolved):
        return resolved
    return None


def find_wineserver() -> str | None:
    # Mirror the resolution *order* of Wine's own loader,
    # dlls/ntdll/unix/loader.c:exec_wineserver() on wine master
    # (https://gitlab.winehq.org/wine/wine/-/blob/master/dlls/ntdll/unix/loader.c
    # #L505), which for an installed Wine is:
    #
    #   1. bin_dir   -- the directory next to the "wine" loader
    #   2. ${WINESERVER}
    #   3. ${PATH}
    #   4. BINDIR    -- the compile-time fallback
    #
    # The two build-tree branches Wine checks before these only apply when
    # running from a Wine build directory, not an installed Wine, so they are
    # intentionally omitted. NOTE: this order has changed across Wine releases;
    # it is pinned to master here and may differ on older Wine.
    #
    # For bin_dir we do not reverse-engineer Wine's layout by guessing lib vs
    # lib64: instead we anchor on the "wine" loader itself. os.path.realpath()
    # follows symlinks -- including /etc/alternatives/wine and winehq's
    # /opt/wine-*/bin/wine -- to the real loader, next to which wineserver is
    # installed in essentially every packaging. Only the architecture-exact
    # multiarch directory and a word-size-ordered lib/lib64 fallback remain as
    # backups for layouts where the two are split (e.g. Debian's wrapper script,
    # which is covered by ${MULTIARCH}).

    seen = set()

    def search(directory):
        candidate = os.path.join(directory, "wineserver")
        if candidate in seen:
            return None
        seen.add(candidate)
        return _resolve_executable_file(candidate)

    wine_loader = shutil.which("wine")
    if wine_loader is not None:
        wine_loader = os.path.realpath(wine_loader)

    prefixes = []
    if wine_loader is not None:
        # e.g. /usr/bin/wine -> /usr
        prefixes.append(os.path.dirname(os.path.dirname(wine_loader)))
    prefixes += ["/usr", "/usr/local"]

    multiarch = sysconfig.get_config_var("MULTIARCH")  # e.g. "x86_64-linux-gnu" on Debian/Ubuntu
    # Generic, non-architecture-specific fallback: try the host's native word
    # size first, so a 64-bit multilib box (where /usr/lib is 32-bit and
    # /usr/lib64 is 64-bit) does not pick up a 32-bit wineserver.
    generic_lib_subdirs = (
        ["lib64/wine", "lib/wine"] if sys.maxsize > 2**32 else ["lib/wine", "lib64/wine"]
    )

    # 1. bin_dir, like Wine -- but anchored on where "wine" actually resolves.
    bin_dirs = []
    if wine_loader is not None:
        bin_dirs.append(os.path.dirname(wine_loader))
    if multiarch:
        bin_dirs += [os.path.join(prefix, "lib", multiarch, "wine") for prefix in prefixes]
    bin_dirs += [
        os.path.join(prefix, subdir) for prefix in prefixes for subdir in generic_lib_subdirs
    ]
    for directory in bin_dirs:
        if (resolved := search(directory)) is not None:
            return resolved

    # 2. ${WINESERVER} -- Wine honors it as a literal path.
    env_wineserver = os.environ.get("WINESERVER")
    if env_wineserver and (resolved := _resolve_executable_file(env_wineserver)) is not None:
        return resolved

    # 3. ${PATH} -- covers a plain "wineserver" on ${PATH}.
    if (path_wineserver := shutil.which("wineserver")) is not None:
        return os.path.realpath(path_wineserver)

    # 4. BINDIR -- Wine's compile-time fallback, conventionally <prefix>/bin.
    for prefix in prefixes:
        if (resolved := search(os.path.join(prefix, "bin"))) is not None:
            return resolved

    return None


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


parse_path_colon_access.__name__ = "PATH:{ro,rw}"  # for argparse


@dataclass
class MountTask:
    mode: MountMode
    target: str
    source: str | None = None
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
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    mount_tasks = [
        MountTask(MountMode.TMPFS, "/"),
        MountTask(MountMode.BIND_RO, "/bin"),
        MountTask(MountMode.DEVTMPFS, "/dev"),
        MountTask(MountMode.BIND_DEV, "/dev/ntsync", required=False),
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
        mount_tasks += [MountTask(MountMode.BIND_RO, pulseaudio_socket)]

    if config.pipewire:
        pipewire_socket = os.path.join(xdg_runtime_dir, "pipewire-0")
        mount_tasks += [MountTask(MountMode.BIND_RO, pipewire_socket)]

    # X11
    if X11Mode(config.x11) != X11Mode.NONE:
        x11_unix_socket = X11Display(config.x11_display_number).get_unix_socket()
        mount_tasks += [MountTask(MountMode.BIND_RO, x11_unix_socket)]
        env_tasks["DISPLAY"] = f":{config.x11_display_number}"

    # Wayland
    if config.wayland:
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        wayland_socket = os.path.join(xdg_runtime_dir, wayland_display)
        env_tasks["WAYLAND_DISPLAY"] = wayland_display
        env_tasks["XDG_RUNTIME_DIR"] = xdg_runtime_dir
        mount_tasks += [MountTask(MountMode.BIND_RO, wayland_socket)]

    # GPU
    if config.nvidia_gpu:
        mount_tasks += [
            MountTask(MountMode.BIND_DEV, "/dev/nvidia0"),
            MountTask(MountMode.BIND_DEV, "/dev/nvidiactl"),
            MountTask(MountMode.BIND_DEV, "/dev/nvidia-modeset"),
        ]

    # Input
    if config.raw_input:
        # default udev based hotplug not working in container
        env_tasks["SDL_JOYSTICK_DISABLE_UDEV"] = "1"
        mount_tasks += [MountTask(MountMode.BIND_DEV, "/dev/input")]

    # Wine
    run_winecfg = X11Mode(config.x11) != X11Mode.NONE and (
        config.configure or config.dotwine is None
    )
    dotwine_target_path = os.path.expanduser("~/.wine")
    if config.dotwine is not None:
        dotwine_source_path, dotwine_access = config.dotwine

        if (
            dotwine_access != AccessMode.READ_ONLY
            and os.path.realpath(dotwine_source_path) == dotwine_target_path
        ):
            raise WineprefixSharingPrevented(
                "Rejected sharing host directory ~/.wine with the sandbox "
                "in read-write mode because it is not secure."
            )

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

    # More Wine: Mount the place that upstream's Debian packages installed to
    #            if(!) that's the Wine we'll be running
    if config.with_wine and (wine_bin_abs_path := shutil.which("wine")) is not None:
        resolved_wine_bin_abs_path = os.path.realpath(wine_bin_abs_path)
        for wine_opt_prefix in (
            "/opt/wine-devel/",
            "/opt/wine-stable/",
            "/opt/wine-staging/",
        ):
            if resolved_wine_bin_abs_path.startswith(wine_opt_prefix):
                mount_tasks += [MountTask(MountMode.BIND_RO, wine_opt_prefix.rstrip("/"))]
                break

    # Even More Wine: Point Wine and the clean-shutdown wrapper at the wineserver
    #                 binary by absolute path. Wine itself honors ${WINESERVER},
    #                 so this removes any reliance on ${PATH} and works regardless
    #                 of the distribution's install location (e.g. Debian/Ubuntu
    #                 multiarch /usr/lib/x86_64-linux-gnu/wine/). The binary may
    #                 live outside the default mount stack (e.g. via a custom
    #                 ${WINESERVER}), so bind-mount it explicitly to guarantee it
    #                 exists inside the sandbox.
    if config.with_wine:
        wineserver_abs_path = find_wineserver()
        if wineserver_abs_path is None:
            raise CommandNotFound("wineserver")
        env_tasks["WINESERVER"] = wineserver_abs_path
        mount_tasks += [MountTask(MountMode.BIND_RO, wineserver_abs_path)]

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
    if config.with_wine:
        # The "wine" loader is launched by bare name and lives next to
        # wineserver (e.g. /usr/lib/wine/ on Ubuntu's wine32:i386, which ships
        # no /usr/bin/wine). Make that directory reachable on the sandbox
        # ${PATH} so `wine` resolves; the filter below keeps it only if it
        # exists in the mount stack. (wineserver itself is invoked by its
        # absolute ${WINESERVER} path, not via ${PATH}.)
        candidate_paths.append(os.path.dirname(wineserver_abs_path))
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
        argv.add(
            "sh",
            "-c",
            '"${WINESERVER}" -p0 && "$0" "$@" ; ret=$? ; "${WINESERVER}" -k ; exit ${ret}',
        )

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
        _logger.error("sandwine requires bubblewrap >=0.8.0, aborting.")
        sys.exit(1)


def require_command_available(command: str):
    if shutil.which(command) is None:
        raise CommandNotFound(command)


def _inner_main(with_wine: bool):
    exit_code = 0
    try:
        config = parse_command_line(sys.argv[1:], with_wine=with_wine)

        coloredlogs.install(level=logging.DEBUG)

        require_command_available("bwrap")

        if config.with_pty:
            # NOTE: Despite being part of util-linux, command "script"
            #       may not be available on Fedora, and it has its own
            #       package "util-linux-script".
            require_command_available("script")

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
                raise CommandNotFound(command=argv[0])

    except KeyboardInterrupt:
        exit_code = 128 + signal.SIGINT

    except WineprefixSharingPrevented as e:
        _logger.error(e)
        exit_code = 1

    except CommandNotFound as e:
        message = f"{str(e)[:-1]}, aborting."
        _logger.error(message)
        exit_code = 127

    sys.exit(exit_code)


def main_sand():
    _inner_main(with_wine=False)


def main_sandwine():
    _inner_main(with_wine=True)
