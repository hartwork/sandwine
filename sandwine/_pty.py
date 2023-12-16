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

import os
import pty


def pty_spawn_argv(argv):
    wait_status = pty.spawn(argv)

    exit_code = os.waitstatus_to_exitcode(wait_status)
    if exit_code < 0:  # e.g. -2 for "killed by SIGINT"
        exit_code = 128 - exit_code  # e.g. -2 -> 130

    return exit_code
