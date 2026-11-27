"""Keeps every test away from the installed server: imported first by every test module (HermeticTest checks that).

bin/update runs these tests on an installed server as its health check, as the administrator. A test that took a
program's default paths there would read the installed UI's state (/var/lib/setup-server-ui, setup-ui 0700), the worker
tree or the library: on Python 3.10 Path.is_dir raises PermissionError on such a directory (3.14 returns False), which
once failed the health check and rolled a production update back. So any test that opens, lists, stats, makes or
removes something of INSTALLED fails at once, whichever module it is in:

- an audit hook for what Python audits (open, listing, mkdir, remove, rename, chmod, rmtree);
- os.stat and os.lstat (not audited: os.path.exists, Path.is_dir and the like) wrapped, pathlib's 3.10 accessor too.
"""

import atexit
import os
import pathlib
from pathlib import Path
import shutil
import sys
import tempfile

INSTALLED = ("/var/lib/setup-server-ui", "/var/lib/private/setup-server-ui", "/srv/setup-server", "/usr/local/lib/setup-server",
             "/etc/setup-server", "/run/vault-handoff")
# Where in-process copies of the programs keep their state instead (load_web_ui, test_test_session's load).
HERMETIC = Path(tempfile.mkdtemp(prefix="setup-server-hermetic-"))
atexit.register(shutil.rmtree, HERMETIC, True)


def touches_installed(path):
    if isinstance(path, int) or not isinstance(path, (str, bytes, os.PathLike)):
        return False
    text = os.path.abspath(os.fsdecode(path))
    return any(text == place or text.startswith(place + "/") for place in INSTALLED)


def refuse_installed(event, arguments):
    """An audit hook: a test that opens, lists, makes or removes anything of the installed server fails at once."""
    if event in ("open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod",
                 "os.chown", "os.utime", "os.symlink", "os.link", "os.truncate", "shutil.rmtree", "shutil.copyfile",
                 "shutil.copytree") and arguments and touches_installed(arguments[0]):
        raise AssertionError(f"a test touched the installed server: {event} {arguments[0]}")


def guarded(function):
    def stat(path, *arguments, **keywords):
        if touches_installed(path):
            raise AssertionError(f"a test touched the installed server: os.{function.__name__} {path}")
        return function(path, *arguments, **keywords)
    stat.__name__ = function.__name__
    stat.hermetic = True
    return stat


if not getattr(os.stat, "hermetic", False):
    sys.addaudithook(refuse_installed)
    os.stat, os.lstat = guarded(os.stat), guarded(os.lstat)
    accessor = getattr(pathlib, "_NormalAccessor", None)  # Python 3.10: pathlib took os.stat when it was imported
    if accessor is not None:
        accessor.stat, accessor.lstat = staticmethod(os.stat), staticmethod(os.lstat)
