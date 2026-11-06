from datetime import timedelta

from sqlalchemy import or_, select, update

from findyou.db import new_id, utcnow
from findyou.errors import AppError
from findyou.ops import Job
from findyou.services import row_dict


class JobQueue:
    def __init__(self, db, lease_seconds: int = 240):
        self.db, self.lease_seconds = db, lease_seconds

    def enqueue(self, kind: str, payload: dict, max_attempts: int = 3) -> dict:
        with self.db.sessions.begin() as session:
            job = Job(kind=kind, payload=payload, max_attempts=max_attempts)
            session.add(job)
            session.flush()
            return row_dict(job)

    def get(self, job_id: str) -> dict:
        with self.db.sessions() as session:
            job = session.get(Job, job_id)
            if not job:
                raise AppError("not_found", "Job not found", 404)
            return row_dict(job)

    def renew(self, job: dict):
        """Fence long bounded read cycles before each network operation."""
        now = utcnow()
        with self.db.sessions.begin() as session:
            result = session.execute(update(Job).where(Job.id == job["id"], Job.status == "running",
                Job.lease_token == job["lease_token"], Job.lease_until > now).values(
                    lease_until=now + timedelta(seconds=self.lease_seconds), updated_at=now))
            if result.rowcount != 1:
                raise AppError("job_lease_lost", "This worker no longer owns the task", 409)

    def claim(self) -> dict | None:
        now = utcnow()
        with self.db.sessions.begin() as session:
            # Expired final attempts cannot stay running forever.
            session.execute(update(Job).where(Job.status == "running", Job.lease_until <= now,
                Job.attempts >= Job.max_attempts).values(status="failed", lease_token=None, lease_until=None,
                error={"code": "lease_expired", "message": "Worker lease expired on final attempt", "retryable": False},
                updated_at=now))
            eligible = or_((Job.status == "queued") & (Job.available_at <= now),
                           (Job.status == "running") & (Job.lease_until <= now))
            candidate = session.scalar(select(Job.id).where(eligible, Job.attempts < Job.max_attempts)
                                       .order_by(Job.created_at, Job.id).limit(1))
            if not candidate:
                return None
            token = new_id()
            result = session.execute(update(Job).where(Job.id == candidate, eligible,
                                     Job.attempts < Job.max_attempts).values(status="running",
                                     attempts=Job.attempts + 1, lease_token=token,
                                     lease_until=now + timedelta(seconds=self.lease_seconds), updated_at=now))
            if result.rowcount != 1:
                return None
            session.expire_all()
            return row_dict(session.get(Job, candidate))

    def finish(self, job: dict, *, result: dict | None = None, error: AppError | None = None):
        now = utcnow()
        retry = bool(error and error.retryable and job["attempts"] < job["max_attempts"])
        values = {"status": "queued" if retry else "failed" if error else "succeeded",
                  "result": result, "error": error.as_dict() if error else None, "lease_until": None,
                  "lease_token": None, "updated_at": now,
                  "available_at": now + timedelta(seconds=min(60, 2 ** job["attempts"]))}
        with self.db.sessions.begin() as session:
            session.execute(update(Job).where(Job.id == job["id"], Job.status == "running",
                                             Job.lease_token == job["lease_token"]).values(**values))
