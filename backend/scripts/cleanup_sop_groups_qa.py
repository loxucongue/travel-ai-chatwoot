"""Remove only this UI test's unused strategies; retain audit and test artifacts."""
from sqlalchemy import select, delete
from app.config import settings
from app.db import SessionLocal
from app.models import User, SopDefinition, SopEnrollment
from app.automation_models import SopVersion, RehearsalEnrollment

assert settings.app_profile == "evaluation" and not settings.outbound_enabled
with SessionLocal() as db:
    user = db.scalar(select(User).where(User.email == "three-modules-qa@example.com"))
    rows = db.scalars(select(SopDefinition).where(SopDefinition.created_by == user.id,
        SopDefinition.name.like("QA-structure-only-groups-%"))).all() if user else []
    removed = []
    for sop in rows:
        versions = db.scalars(select(SopVersion.id).where(SopVersion.sop_id == sop.id)).all()
        assert not db.scalar(select(SopEnrollment.id).where(SopEnrollment.sop_id == sop.id)), "enrolled_strategy_not_removed"
        assert not db.scalar(select(RehearsalEnrollment.id).where(RehearsalEnrollment.sop_version_id.in_(versions))), "rehearsal_strategy_not_removed"
        db.execute(delete(SopVersion).where(SopVersion.sop_id == sop.id))
        removed.append(sop.id)
        db.delete(sop)
    db.commit()
    print({"removed_qa_strategy_ids": removed})
