"""The monitor's window: draws what ``follower.BufferMonitor`` reports.

Polls the model on a timer, draws what it gets, and turns widget events
(the slider, follow, pause, refresh) into calls on the model.
"""

# Deliberately thin: this module cannot be tested on a machine with no
# display, so every decision belongs in follower.py, which can be. Imported
# only by __main__ -- the model itself never imports Qt, which is what lets
# the monitor run on a different interpreter from the engine.

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from stacscope.monitor.colormap import magma_lut
from stacscope.monitor.follower import BufferMonitor, State, View

REDRAW_MS = 33
"""About 30 Hz. The producer is not slowed by being watched -- nothing here
touches it -- so this is only how often the window asks."""


class MonitorWindow(QtWidgets.QMainWindow):
    def __init__(self, monitor: BufferMonitor) -> None:
        super().__init__()
        self.monitor = monitor
        self._levels_set = False
        self.setWindowTitle(f"stacscope buffer monitor - {monitor.name}")

        self.image = pg.ImageView()
        self.image.setColorMap(
            pg.ColorMap(pos=np.linspace(0.0, 1.0, 256), color=magma_lut())
        )

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.valueChanged.connect(self._slider_moved)

        self.follow = QtWidgets.QCheckBox("Live follow")
        self.follow.setChecked(monitor.follow)
        self.follow.toggled.connect(self._follow_toggled)

        self.pause = QtWidgets.QCheckBox("Pause")
        self.pause.toggled.connect(self._pause_toggled)

        self.refresh = QtWidgets.QPushButton("Refresh")
        self.refresh.setToolTip(
            "Re-read the slot now: resolves a torn image, or takes one update "
            "while paused."
        )
        self.refresh.clicked.connect(self._refresh_clicked)

        self.status = QtWidgets.QLabel("waiting for a buffer...")
        self.header_table = _table("Header")
        self.meta_table = _table("Frame")

        self.setCentralWidget(self._layout())

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REDRAW_MS)

    # --- layout ---------------------------------------------------------------

    def _layout(self) -> QtWidgets.QWidget:
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Slot"))
        controls.addWidget(self.slider, stretch=1)
        controls.addWidget(self.follow)
        controls.addWidget(self.pause)
        controls.addWidget(self.refresh)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.image, stretch=1)
        left.addLayout(controls)
        left.addWidget(self.status)

        tables = QtWidgets.QVBoxLayout()
        tables.addWidget(self.header_table, stretch=1)
        tables.addWidget(self.meta_table)  # sized to its rows by _fit, below

        split = QtWidgets.QHBoxLayout()
        split.addLayout(left, stretch=3)
        split.addLayout(tables, stretch=1)

        panel = QtWidgets.QWidget()
        panel.setLayout(split)
        return panel

    # --- events ---------------------------------------------------------------

    def _slider_moved(self, value: int) -> None:
        if self.monitor.follow:  # moving the slider means "watch this instead"
            self.follow.setChecked(False)
        self.monitor.seek(value)
        self._draw(self.monitor.refresh())

    def _follow_toggled(self, on: bool) -> None:
        self.monitor.follow = on

    def _pause_toggled(self, on: bool) -> None:
        self.monitor.paused = on

    def _refresh_clicked(self) -> None:
        self._draw(self.monitor.refresh())

    def _tick(self) -> None:
        self._draw(self.monitor.poll())

    # --- drawing --------------------------------------------------------------

    def _draw(self, view: View | None) -> None:
        self._sync_slider()
        if view is None:
            if self.monitor.state is State.WAITING:
                self.status.setText(f"waiting for a buffer named {self.monitor.name!r}...")
            return

        # autoLevels only for the first frame: after that the levels are the
        # user's, set on the histogram, and must not be snatched back.
        self.image.setImage(
            view.image.T,
            autoLevels=not self._levels_set,
            autoRange=not self._levels_set,
            autoHistogramRange=not self._levels_set,
        )
        self._levels_set = True

        if self.slider.value() != view.slot:
            _quietly(self.slider, view.slot)
        self.status.setText(_describe(view))
        _fill(self.header_table, self.monitor.header().items())
        _fill(self.meta_table, view.meta.items())
        _fit(self.meta_table)

    def _sync_slider(self) -> None:
        slots = self.monitor.slots()
        if slots is not None and self.slider.maximum() != slots - 1:
            self.slider.setMaximum(slots - 1)


def _describe(view: View) -> str:
    frame = "no frame of this acquisition" if view.index < 0 else f"frame {view.index}"
    torn = "" if view.intact else "   TORN: the producer is writing this slot"
    return (
        f"slot {view.slot}   {frame}   acquisition {view.acquisition_id}   "
        f"published {view.published}{torn}"
    )


def _table(title: str) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, 2)
    table.setHorizontalHeaderLabels([title, "value"])
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return table


def _fill(table: QtWidgets.QTableWidget, items) -> None:
    items = list(items)
    if table.rowCount() != len(items):
        table.setRowCount(len(items))
    for row, (name, value) in enumerate(items):
        table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(name)))
        table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))


def _fit(table: QtWidgets.QTableWidget) -> None:
    """Shrink a table to exactly the rows it holds.

    The frame metadata is a handful of fields where the header is twenty-odd,
    so left to share the column evenly it sits in a tall box of empty grid.
    Capping it at its content height gives the space to the header table,
    which can use it."""
    table.resizeRowsToContents()
    height = table.horizontalHeader().height() + 2 * table.frameWidth()
    height += sum(table.rowHeight(row) for row in range(table.rowCount()))
    table.setMaximumHeight(height)


def _quietly(slider: QtWidgets.QSlider, value: int) -> None:
    """Move the handle without it looking like the user did."""
    slider.blockSignals(True)
    slider.setValue(value)
    slider.blockSignals(False)


def run(monitor: BufferMonitor) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MonitorWindow(monitor)
    window.resize(1100, 700)
    window.show()
    return app.exec()
