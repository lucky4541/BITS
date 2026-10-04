"""Steps 38-45 (spec: "LEVEL-BASED REAL-TIME PROGRESS") - a real,
level-weighted progress model used ONLY by this module (never touches
Zoning/Validation/Comparison's own, separate progress systems). Every
number this produces is derived from actual completed/total work handed
to it by the orchestrator - it never fabricates a percentage or ETA."""
import time
from dataclasses import dataclass, field

LEVELS = [
    (1, "Project Discovery"),
    (2, "Content Analysis"),
    (3, "Navigation & Linking"),
    (4, "Package Generation"),
    (5, "Validation & Auto-Repair"),
    (6, "Final Verification"),
]

# Configurable (spec 42) - must sum to 1.0.
DEFAULT_LEVEL_WEIGHTS = {1: 0.10, 2: 0.25, 3: 0.25, 4: 0.15, 5: 0.15, 6: 0.10}


@dataclass
class ProgressState:
    level: int = 0
    level_name: str = ""
    operation: str = ""
    current_file: str = ""
    completed: int = 0
    total: int = 0
    overall_pct: float = 0.0
    elapsed_seconds: float = 0.0
    estimated_remaining_seconds: float = None   # None = "Calculating..." (spec 44)
    cancelled: bool = False
    level_status: dict = field(default_factory=dict)   # level_number -> "done"/"active"/"pending"
    stage_timings: dict = field(default_factory=dict)   # level_name -> seconds, filled in as each level finishes


class ProgressTracker:
    """One instance per orchestrator run. `on_update(ProgressState)` is
    called on every real progress event - the GUI's job is to marshal that
    call onto the Tk main thread (core.epub_structure never touches Tk
    itself). Overall percentage never moves backwards (spec 42) - each
    level's own internal 0..1 fraction is clamped and only ever combined
    with already-completed earlier levels' full weights."""

    def __init__(self, on_update=None, weights: dict = None, cancel_check=None):
        self.on_update = on_update or (lambda state: None)
        self.weights = weights or dict(DEFAULT_LEVEL_WEIGHTS)
        self.cancel_check = cancel_check or (lambda: False)
        self._start_time = time.monotonic()
        self._level_start_time = self._start_time
        self._completed_weight = 0.0
        self._state = ProgressState(level_status={n: "pending" for n, _ in LEVELS})
        self._rate_samples = []   # [(elapsed, completed_weight), ...] for ETA

    def start_level(self, level: int):
        if level > 1:
            self._finish_level(level - 1)
        name = dict(LEVELS)[level]
        for n in self._state.level_status:
            if n < level:
                self._state.level_status[n] = "done"
            elif n == level:
                self._state.level_status[n] = "active"
            else:
                self._state.level_status[n] = "pending"
        self._state.level = level
        self._state.level_name = name
        self._level_start_time = time.monotonic()
        self._emit(operation=f"Starting {name}", completed=0, total=0)

    def _finish_level(self, level: int):
        name = dict(LEVELS).get(level)
        if name and name not in self._state.stage_timings:
            self._state.stage_timings[name] = round(time.monotonic() - self._level_start_time, 2)
        self._completed_weight += self.weights.get(level, 0.0)

    def update(self, operation: str, completed: int = 0, total: int = 0, current_file: str = ""):
        self._emit(operation=operation, completed=completed, total=total, current_file=current_file)

    def _emit(self, operation: str, completed: int, total: int, current_file: str = ""):
        level_fraction = (completed / total) if total > 0 else 0.0
        level_fraction = max(0.0, min(1.0, level_fraction))
        overall = self._completed_weight + level_fraction * self.weights.get(self._state.level, 0.0)
        overall = max(overall, self._state.overall_pct)   # never move backwards
        overall = min(1.0, overall)   # a percentage can never exceed 100% - see finish()'s own docstring

        elapsed = time.monotonic() - self._start_time
        self._rate_samples.append((elapsed, overall))
        if len(self._rate_samples) > 20:
            self._rate_samples.pop(0)
        remaining = None
        if overall > 0.02 and len(self._rate_samples) >= 2:
            t0, p0 = self._rate_samples[0]
            dt, dp = elapsed - t0, overall - p0
            if dp > 1e-6 and dt > 1e-6:
                rate = dp / dt
                remaining = max(0.0, (1.0 - overall) / rate)

        self._state.operation = operation
        self._state.completed = completed
        self._state.total = total
        self._state.current_file = current_file
        self._state.overall_pct = overall
        self._state.elapsed_seconds = elapsed
        self._state.estimated_remaining_seconds = remaining
        self._state.cancelled = self.cancel_check()
        self.on_update(self._state)

    def finish(self):
        self._finish_level(self._state.level)
        # _finish_level() just credited the LAST level's own full weight
        # into _completed_weight - clear self._state.level to a number no
        # weight is registered for, so the _emit() call below never adds
        # that same level's weight a second time on top of it.
        self._state.level = 0
        for n in self._state.level_status:
            self._state.level_status[n] = "done"
        self._emit(operation="Complete", completed=1, total=1)


def format_duration(seconds) -> str:
    if seconds is None:
        return "Calculating..."
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
