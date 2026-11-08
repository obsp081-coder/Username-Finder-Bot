"""Deterministic desktop substitute; never sends operating-system input."""

from username_finder.snapscore_desktop import DesktopError, Target

TARGET = Target(123, 456, "Snapchat", "msedge.exe", (100, 100, 1100, 800), 120, (0, 0, 2560, 1440))
POINTS = ((200, 200), (300, 300), (400, 400))


class FakeDesktop:
    available = True

    def __init__(self):
        self.target = TARGET
        self.point = POINTS[0]
        self.clicks = []
        self.captures = 0
        self.callback = None
        self.disarmed = 0
        self.problem = self.click_problem = self.arm_problem = None

    def capture(self):
        self.captures += 1
        if self.problem:
            raise DesktopError(self.problem)
        return self.target, self.point

    def validate(self, target, point):
        if self.problem:
            raise DesktopError(self.problem)
        if target != self.target:
            raise DesktopError("changed")
        if not target.contains(point):
            raise DesktopError("point")

    def arm_stop(self, callback):
        if self.arm_problem:
            raise DesktopError(self.arm_problem)
        self.callback = callback

    def disarm_stop(self):
        self.disarmed += 1
        self.callback = None

    def click(self, target, point):
        self.validate(target, point)
        if self.click_problem:
            raise self.click_problem
        self.clicks.append(point)


def calibrate(panel):
    for index, point in enumerate(POINTS):
        panel.desktop.point = point
        panel.calibrate(index)
        panel.capture_due = 0
        panel._capture_tick()


def advance_clock(session):
    now = session.due
    session.clock = lambda: now
    session.tick()
