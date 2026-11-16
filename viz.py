#!/usr/bin/env python3
"""PyQt timeline visualizer for SAF-Scheduler M:N trace output.

Parses lines of the form:
    TRACE,<timestamp_ns>,<worker_id>,<thread_id>,<START|YIELD|EXIT>
"""

import os
import subprocess
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
QT_ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter
EXEC_APP = lambda app: app.exec()


PALETTE = [
    QColor("#89B4FA"),  # Blue
    QColor("#A6E3A1"),  # Green
    QColor("#FAB387"),  # Peach
    QColor("#F38BA8"),  # Red
    QColor("#CBA6F7"),  # Mauve
    QColor("#94E2D5"),  # Teal
    QColor("#F9E2AF"),  # Yellow
    QColor("#74C7EC"),  # Sapphire
]


def color_for_thread(tid: int) -> QColor:
    return PALETTE[(tid - 1) % len(PALETTE)]


def parse_trace(text: str):
    """Return list of sorted (worker_id, tid, start_ns, end_ns, end_event)."""
    slices = []
    active = {}  # (worker_id, tid) -> start_ns

    for raw in text.splitlines():
        if not raw.startswith("TRACE,"):
            continue
        parts = raw.strip().split(",")
        if len(parts) != 5:
            continue

        _, ts_s, wid_s, tid_s, ev = parts
        ts = int(ts_s)
        wid = int(wid_s)
        tid = int(tid_s)
        key = (wid, tid)

        if ev == "START":
            active[key] = ts
        elif ev in ("YIELD", "EXIT") and key in active:
            start_ts = active.pop(key)
            slices.append((wid, tid, start_ts, max(ts, start_ts), ev))

    # Sort globally by real start timestamp so order of execution is correct
    slices.sort(key=lambda x: x[2])
    return slices


class TimelineCanvas(QWidget):
    def __init__(self):
        super().__init__()
        self.slices = []
        self.visible_count = 0
        self.step_mode = True
        self.setMinimumHeight(340)

    def set_data(self, slices):
        self.slices = slices
        self.visible_count = 0
        self.update()

    def set_step_mode(self, enabled: bool):
        self.step_mode = enabled
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1E1E2E"))

        if not self.slices:
            painter.setPen(QColor("#CDD6F4"))
            painter.setFont(QFont("Monospace", 11))
            painter.drawText(
                self.rect(),
                QT_ALIGN_CENTER,
                "Click 'Run ./scheduler' to capture and visualize scheduling slices",
            )
            return

        # Use max_workers configured in the app UI, or at least all workers seen in trace
        num_workers = getattr(self, "configured_workers", len(set(s[0] for s in self.slices)))
        workers = list(range(max(num_workers, 1)))

        t_min = min(s[2] for s in self.slices)
        t_max = max(s[3] for s in self.slices)
        total_ns = max(t_max - t_min, 1)

        margin_left, margin_top, margin_right = 110, 45, 30
        width = max(self.width() - margin_left - margin_right, 100)
        row_h = 48
        row_gap = 20

        needed_height = margin_top + len(workers) * (row_h + row_gap) + 20
        if self.minimumHeight() != needed_height:
            self.setMinimumHeight(needed_height)

        painter.setFont(QFont("Monospace", 10))
        for idx, wid in enumerate(workers):
            y = margin_top + idx * (row_h + row_gap)
            painter.setPen(QColor("#CDD6F4"))
            painter.drawText(15, y + row_h // 2 + 5, f"Worker {wid}")
            painter.setPen(QColor("#313244"))
            painter.drawLine(margin_left, y + row_h // 2, margin_left + width, y + row_h // 2)

        shown = self.slices[: self.visible_count]

        if self.step_mode:
            # Per-worker step positioning: track step index for each worker
            worker_step = {w: 0 for w in workers}
            worker_total = {w: 0 for w in workers}
            for s in self.slices:
                worker_total[s[0]] += 1

            for wid, tid, start, end, ev in shown:
                row = workers.index(wid)
                y = margin_top + row * (row_h + row_gap)
                w_tot = max(worker_total[wid], 1)
                step_w = max(width / w_tot, 12.0)

                x1 = margin_left + int(worker_step[wid] * step_w)
                bar_w = max(int(step_w) - 3, 8)
                worker_step[wid] += 1

                color = color_for_thread(tid)
                painter.fillRect(x1, y, bar_w, row_h, color)
                painter.setPen(QColor("#11111B"))
                painter.drawRect(x1, y, bar_w, row_h)

                if bar_w >= 20:
                    label = f"T{tid}" if ev == "YIELD" else f"T{tid}*"
                    painter.drawText(x1 + 4, y + row_h // 2 + 4, label)
        else:
            # Time-scaled positioning
            for wid, tid, start, end, ev in shown:
                row = workers.index(wid)
                y = margin_top + row * (row_h + row_gap)

                x1 = margin_left + int(((start - t_min) / total_ns) * width)
                x2 = margin_left + int(((end - t_min) / total_ns) * width)
                bar_w = max(x2 - x1, 6)

                color = color_for_thread(tid)
                painter.fillRect(x1, y, bar_w, row_h, color)
                painter.setPen(QColor("#11111B"))
                painter.drawRect(x1, y, bar_w, row_h)

                if bar_w >= 20:
                    label = f"T{tid}" if ev == "YIELD" else f"T{tid}*"
                    painter.drawText(x1 + 4, y + row_h // 2 + 4, label)


class SchedulerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SAF-Scheduler M:N Timeline Visualizer")
        self.resize(960, 440)

        self.canvas = TimelineCanvas()
        self.status = QLabel("Ready")

        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 16)
        self.spin_workers.setValue(2)
        self.spin_workers.setPrefix("Workers: ")

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 128)
        self.spin_threads.setValue(3)
        self.spin_threads.setPrefix("Threads: ")

        self.spin_iters = QSpinBox()
        self.spin_iters.setRange(1, 50)
        self.spin_iters.setValue(4)
        self.spin_iters.setPrefix("Iters: ")

        self.chk_step = QCheckBox("Step-order view")
        self.chk_step.setChecked(True)
        self.chk_step.toggled.connect(self.canvas.set_step_mode)

        self.btn_run = QPushButton("Run ./scheduler")
        self.btn_run.clicked.connect(self.run_scheduler)

        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self.toggle_pause)

        controls = QHBoxLayout()
        controls.addWidget(self.btn_run)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.spin_workers)
        controls.addWidget(self.spin_threads)
        controls.addWidget(self.spin_iters)
        controls.addWidget(self.chk_step)
        controls.addWidget(self.status)
        controls.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(controls)
        layout.addWidget(self.canvas)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.timer = QTimer()
        self.timer.timeout.connect(self.step_animation)

    def run_scheduler(self):
        self.timer.stop()
        binary = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduler")
        if not os.path.exists(binary):
            self.status.setText("Error: './scheduler' not found. Run 'make' first.")
            return

        cmd = [
            binary,
            "-w",
            str(self.spin_workers.value()),
            "-n",
            str(self.spin_threads.value()),
            "-t",
            str(self.spin_iters.value()),
        ]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            self.status.setText(f"Scheduler failed (exit {exc.returncode})")
            return

        slices = parse_trace(out)
        self.canvas.configured_workers = self.spin_workers.value()
        self.canvas.set_data(slices)
        self.btn_pause.setText("Pause")
        self.status.setText(
            f"{self.spin_threads.value()} threads on "
            f"{self.spin_workers.value()} workers — {len(slices)} slices"
        )
        if slices:
            self.timer.start(120)

    def toggle_pause(self):
        if self.timer.isActive():
            self.timer.stop()
            self.btn_pause.setText("Resume")
        elif self.canvas.visible_count < len(self.canvas.slices):
            self.timer.start(120)
            self.btn_pause.setText("Pause")

    def step_animation(self):
        if self.canvas.visible_count < len(self.canvas.slices):
            self.canvas.visible_count += 1
            self.canvas.update()
        else:
            self.timer.stop()
            self.btn_pause.setText("Pause")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SchedulerWindow()
    win.show()
    sys.exit(EXEC_APP(app))
