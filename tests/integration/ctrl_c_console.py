"""Run by ``ctrl_c_support`` on Windows, in a hidden console of its own.

Starts the command it is given in that console, then presses Ctrl-C there
each time it reads "press" on its input; "kill" kills the command. Windows sends a console's Ctrl-C to
every process attached to it, so pressing it here reaches the command and
whatever the command started -- as an operator's would -- and nothing of the
test's.

Exits with the command's exit status.
"""

import ctypes
import subprocess
import sys
import threading

CTRL_C_EVENT = 0

kernel32 = ctypes.windll.kernel32
# Ignoring Ctrl-C is inherited, and whoever started the test may have set it
# (an IDE's or an agent's shell often does): turn it off, so the command
# starts with it off too.
kernel32.SetConsoleCtrlHandler(None, False)
command = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.STDOUT)
# And on for this host, now the command has started: it must survive its own
# presses.
kernel32.SetConsoleCtrlHandler(None, True)


def presses() -> None:
    for line in sys.stdin:
        match line.strip():
            case "press":
                kernel32.GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)
            case "kill":  # a test's cleanup: killing this host would orphan the command
                command.kill()


threading.Thread(target=presses, daemon=True).start()
sys.exit(command.wait())
