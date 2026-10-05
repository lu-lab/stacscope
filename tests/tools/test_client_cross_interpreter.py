"""WS23 -- the client on the other interpreter from the engine.

The engine runs on free-threaded Python; a script or a Pycro-Manager hook
may well run on the standard build, in an environment of its own. The client
must work across that pairing: it shares nothing with the engine but the
socket and the frames, so this is what proves the frames carry everything.

Not in the default suite, for the reasons ``test_cross_interpreter.py``
gives: it needs both interpreters, and builds an environment of its own.

    uv run pytest tests/tools -q
"""

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

# The engine-in-a-thread helper is the integration suite's; this directory
# is collected on its own.
sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
from gateway_support import EngineThread, rig_text  # noqa: E402

OTHER = "3.14" if not sys._is_gil_enabled() else "3.14t"

SCRIPT = textwrap.dedent("""
    import json, sys
    import numpy as np
    from stacscope.client import Client
    from stacscope.core.protocol import SetWaveform, Waveform

    with Client({url!r}) as engine:
        quiet = np.zeros((1, 4)), np.zeros((1, 4), dtype=bool)
        do = np.zeros((1, 50), dtype=bool)
        do[0, 0] = True
        source = engine.source()
        for name, (ao, d) in [(Waveform.WARMUP, quiet), (Waveform.MAIN, (np.zeros((1, 50)), do)),
                              (Waveform.COOLDOWN, quiet)]:
            engine.ask(source, SetWaveform(name, ao, d))
        started = engine.start(cycles=-1)
        running = engine.status()
        stopped = engine.stop()
    print(json.dumps({{
        "gil": sys._is_gil_enabled(),
        "started": started,
        "state": running.state.value,
        "devices": [d.name for d in running.devices],
        "stopped": stopped,
    }}))
""")


def _has_other() -> bool:
    found = subprocess.run(["uv", "python", "find", OTHER], capture_output=True, text=True)
    return found.returncode == 0


@pytest.mark.timeout(900)
@pytest.mark.skipif(not _has_other(), reason=f"no {OTHER} interpreter on this machine")
def test_ws23_a_client_on_the_other_interpreter_runs_an_acquisition(tmp_path):
    engine = EngineThread(rig_text(uuid.uuid4().hex[:12]))
    try:
        # An environment of its own, well away from the one running this test.
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(tmp_path / "env"), "VIRTUAL_ENV": ""}
        result = subprocess.run(
            ["uv", "run", "--python", OTHER, "python", "-c", SCRIPT.format(url=engine.url)],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    finally:
        engine.close()

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["gil"] is not sys._is_gil_enabled()  # or this proves nothing
    assert report["state"] == "running"
    assert report["devices"] == ["source", "cam"]
    assert report["started"] == report["stopped"] == 1
