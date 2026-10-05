"""``stacscope serve`` in a process of its own, where a real Ctrl-C can be pressed.

A test cannot press Ctrl-C on its own console: it would reach the test
runner too. So the engine gets one of its own. On Windows that is a hidden
console, with ``ctrl_c_console.py`` in it to press the key; on POSIX, a
session of its own, whose process group is signalled as a terminal would
signal its foreground group.
"""

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent


class Serve:
    """``stacscope serve --config config``, running until pressed or killed.
    Its device classes are looked up in this directory too."""

    def __init__(self, config: Path) -> None:
        paths = [str(HERE), *filter(None, [os.environ.get("PYTHONPATH")])]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
        command = [sys.executable, "-m", "stacscope.app.cli", "serve", "--config", str(config)]
        if os.name == "nt":
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0  # SW_HIDE
            self._process = subprocess.Popen(
                [sys.executable, str(HERE / "ctrl_c_console.py"), *command],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=info,
                env=env,
                text=True,
            )
        else:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
                text=True,
            )
        self.output: list[str] = []
        self._lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        # A daemon: the pipe stays open for as long as anything the engine
        # started holds it, which is what some of these tests are about.
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self._process.stdout:
            self.output.append(line.rstrip("\n"))
            self._lines.put(line)
        self._lines.put(None)

    def expect(self, text: str, timeout: float) -> str:
        """The next line of output containing ``text``. Fails the test if none
        comes within ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(left, 0))
            except queue.Empty:
                line = None
            if line is None:
                raise AssertionError(f"no line saying {text!r}; the output was:\n" + "\n".join(self.output))
            if text in line:
                return line

    def url(self, timeout: float) -> str:
        line = self.expect("listening on", timeout)
        return line[line.index("ws://") :].split()[0]

    def press(self) -> None:
        """Ctrl-C, as an operator presses it at the engine's console."""
        if os.name == "nt":
            self._process.stdin.write("press\n")
            self._process.stdin.flush()
        else:
            os.killpg(self._process.pid, signal.SIGINT)

    def wait(self, timeout: float) -> int:
        """The engine's exit status, once it has exited."""
        return self._process.wait(timeout)

    def close(self) -> None:
        if self._process.poll() is None and os.name == "nt":
            try:
                self._process.stdin.write("kill\n")  # the engine, not just its console's host
                self._process.stdin.flush()
                self._process.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def __enter__(self) -> "Serve":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
