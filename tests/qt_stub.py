"""Worker-only substitute when Qt is absent; this does NOT test Qt widgets."""
import sys
import threading
import types


class Signal:
    def __init__(self, *args):
        self.name = ""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return instance.__dict__.setdefault(self.name, BoundSignal())


class BoundSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class Thread:
    finished = Signal()

    def __init__(self):
        self.thread = None

    def start(self):
        def execute():
            try:
                self.run()
            finally:
                self.finished.emit()
        self.thread = threading.Thread(target=execute)
        self.thread.start()

    def isRunning(self):
        return bool(self.thread and self.thread.is_alive())

    def wait(self, milliseconds=5000):
        self.thread.join(milliseconds / 1000)


def install():
    qt = types.ModuleType("PyQt6")
    core = types.ModuleType("PyQt6.QtCore")
    core.QThread, core.pyqtSignal = Thread, Signal
    sys.modules.update({"PyQt6": qt, "PyQt6.QtCore": core})
