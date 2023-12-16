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

import fcntl
import os
import pty
import signal
import struct
import termios
from functools import partial
from unittest.mock import patch

import pexpect


def _copy_window_size(to_p: pexpect.spawn, from_fd: int):
    if not os.isatty(from_fd):
        return

    s = struct.pack('HHHH', 0, 0, 0, 0)
    a = struct.unpack('HHHH', fcntl.ioctl(from_fd, termios.TIOCGWINSZ, s))
    if not to_p.closed:
        to_p.setwinsize(a[0], a[1])


def _handle_sigwinch(p: pexpect.spawn, *_):
    _copy_window_size(to_p=p, from_fd=pty.STDOUT_FILENO)


def pty_spawn_argv(argv):
    # NOTE: This implementation is known to not be the real deal,
    #       e.g. Ctrl+Z will not work at all.  It's just a cheap win over
    #       the previous implementation before the real deal.
    p = pexpect.spawn(command=argv[0], args=argv[1:], timeout=None)

    signal.signal(signal.SIGWINCH, partial(_handle_sigwinch, p))

    _copy_window_size(p, from_fd=pty.STDOUT_FILENO)

    if os.isatty(pty.STDIN_FILENO):
        p.interact()
    else:
        with patch('tty.tcgetattr'), patch('tty.tcsetattr'), patch('tty.setraw'):
            p.interact()

    p.wait()
    p.close()

    if p.signalstatus is not None:
        exit_code = 128 + p.signalstatus
    else:
        exit_code = p.exitstatus

    return exit_code
