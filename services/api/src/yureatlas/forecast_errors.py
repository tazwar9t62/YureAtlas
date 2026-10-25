"""Structured forecast outcomes, independent of human-readable error wording."""

from .timeutil import utc


class PublicationDeadlineMissed(ValueError):
    code = "publication_deadline_missed"

    def __init__(self, start, stage, checked_at):
        self.result = {
            "code": self.code,
            "start": utc(start).isoformat(),
            "stage": stage,
            "checked_at": utc(checked_at).isoformat(),
        }
        super().__init__(f"Missed publication deadline {stage}; nothing published")


class LegacyRetryIdentityUnknown(ValueError):
    code = "legacy_retry_identity_unknown"

    def __init__(self, job_id):
        reason = (
            "The prior attempt did not archive a complete forecast target and model identity; "
            "refusing to infer or republish it"
        )
        self.result = {"code": self.code, "job_id": job_id, "reason": reason}
        super().__init__(reason)
