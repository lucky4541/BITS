"""Generic, local, operation-level undo/redo for the Verification
window's own edits - never OCR, never a verifier call, never a PDF
re-render (the caller is trusted to only ever push an Operation whose
undo/redo closures satisfy that; this module just manages the stack
discipline).

Every local mutation the Verification window already performs (style
apply, manual text edit, applied fix, page-number correction, zone
merge/split) pushes ONE Operation here, capturing enough to reverse
itself: a snapshot-based approach (store the old and new state directly)
rather than a generic command-pattern re-execution, since every mutation
this window makes is already cheap, local, and side-effect-free beyond
the one zone/session it touches - there is nothing to "replay", only
state to restore."""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Operation:
    """`undo`/`redo` are zero-argument callables that restore the exact
    prior/new state - built by the caller as closures over whatever
    state they captured (e.g. "set this zone's spans back to X").
    `description` is a short, human-readable label for the operation
    (used nowhere functionally yet, but is what a future "Undo: Applied
    Italic" tooltip would show)."""
    description: str
    undo: Callable[[], None]
    redo: Callable[[], None]


class UndoStack:
    def __init__(self, max_size: int = 200):
        self._undo_stack = []
        self._redo_stack = []
        self._max_size = max_size

    def push(self, operation: Operation):
        """Recording a NEW operation always clears the redo branch (spec:
        "If Undo is pressed and then a new edit is made, clear the
        invalid Redo branch using normal editor behavior") - this is the
        one, single place that happens, so no caller needs to remember
        to do it themselves."""
        self._undo_stack.append(operation)
        self._redo_stack.clear()
        if len(self._undo_stack) > self._max_size:
            self._undo_stack.pop(0)

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self) -> Optional[Operation]:
        """Undoes exactly the ONE most recent operation (spec: "Undo must
        restore ONLY the selected operation... reverse order") and moves
        it to the redo stack. No-op (returns None) when nothing to undo."""
        if not self._undo_stack:
            return None
        operation = self._undo_stack.pop()
        operation.undo()
        self._redo_stack.append(operation)
        return operation

    def redo(self) -> Optional[Operation]:
        if not self._redo_stack:
            return None
        operation = self._redo_stack.pop()
        operation.redo()
        self._undo_stack.append(operation)
        return operation

    def clear(self):
        self._undo_stack.clear()
        self._redo_stack.clear()
