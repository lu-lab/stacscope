"""B8 — a reader in another process, while the producer is writing.

Every frame the reader keeps must be exactly the frame its index names: no
torn frames, no frames from the wrong slot. Frames it could not keep up
with must be reported, so read + lost always accounts for every frame.
"""

import json
import subprocess
import sys
import textwrap
import time

from buffer_support import produce
from stacscope.core.buffer import FrameBuffer, Layout, MetaField, Metadata

TOTAL = 3000

READER = """
    import json, sys, time
    from stacscope.core.buffer import FrameBuffer, Overrun

    reader = FrameBuffer.open({name!r})
    cursor = reader.cursor(reader.oldest())
    print("ready", flush=True)
    kept, lost, bad, stalled = 0, 0, [], False
    deadline = time.monotonic() + 6
    while cursor.position < {total} and time.monotonic() < deadline:
        try:
            frame = cursor.next()
        except Overrun as gap:
            lost += gap.skipped
            continue
        if frame is None:
            time.sleep(0)
            continue
        if frame.index == {total} // 3 and not stalled:
            stalled = True
            time.sleep(0.05)  # a reader that falls behind: the producer laps it
        pixels = frame.data.copy()
        if not reader.still_valid(frame.index):
            lost += 1  # overwritten while copying: counted, not kept
            continue
        kept += 1
        if not (pixels == frame.index).all() or int(frame.meta["sequence"]) != frame.index:
            bad.append(frame.index)
    del frame, pixels
    reader.close()
    print(json.dumps({{"kept": kept, "lost": lost, "bad": bad[:10], "end": cursor.position}}))
"""


def test_a_reader_in_another_process_keeps_only_whole_correct_frames(make_writer, name):
    writer = make_writer()
    writer.configure(
        Layout(shape=(64, 64), dtype="<u4", n_slots=16), Metadata(fields=MetaField.HOST_TIME)
    )
    writer.start_acquisition()

    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(READER.format(name=name, total=TOTAL))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready", child.stderr.read()
        for start in range(0, TOTAL, 10):
            produce(writer, 10)  # bursts, with no waiting on the reader
            time.sleep(0)
        out, err = child.communicate(timeout=8)
    finally:
        child.kill()

    assert child.returncode == 0, err
    report = json.loads(out.strip().splitlines()[-1])
    assert report["bad"] == [], "a kept frame was torn or from the wrong slot"
    assert report["end"] == TOTAL
    assert report["kept"] + report["lost"] == TOTAL
    assert report["kept"] > 0
    assert report["lost"] > 0, "the reader was never lapped: the overrun path went untested"
