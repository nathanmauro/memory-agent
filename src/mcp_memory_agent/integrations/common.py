"""Shared helpers for client integration installers."""

import os
import shlex
import shutil
import sys


def entry_point_command(script_name: str, module_name: str) -> str:
    local_script = os.path.join(os.path.dirname(sys.executable), script_name)
    if os.path.exists(local_script):
        return shlex.quote(local_script)

    installed = shutil.which(script_name)
    if installed:
        return shlex.quote(installed)

    return f"{shlex.quote(sys.executable)} -m {module_name}"

