"""(De)serialization of a VerificationSession to/from the existing
project JSON, following the EXACT precedent core/project_manager.py's
own `page_rotations` key already established: one new, optional,
`.setdefault()`-backfilled top-level key, never a parallel project file.

No changes to core/zone_manager.py's Zone schema are needed - a
VerificationIssue's own `zone_id` field is enough to associate it back
to its zone; nothing needs to be duplicated into Zone.attributes."""
from core.verification.verification_models import VerificationSession

PROJECT_KEY = "verification"


def session_to_project_value(session: "VerificationSession | None") -> dict:
    if session is None:
        return {}
    return session.to_dict()


def session_from_project_data(data: dict) -> VerificationSession:
    return VerificationSession.from_dict(data.get(PROJECT_KEY, {}))
