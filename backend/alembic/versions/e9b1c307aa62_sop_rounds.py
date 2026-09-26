"""Persist customer SOP rounds independently from AI generations."""
from alembic import op
import sqlalchemy as sa

revision = "e9b1c307aa62"
down_revision = "3352b5e45e19"
branch_labels = None
depends_on = None


def upgrade():
    for column in (sa.Column("sop_id", sa.Integer()), sa.Column("subject_key", sa.String(160)),
                   sa.Column("round_number", sa.Integer()), sa.Column("request_key", sa.String(80)),
                   sa.Column("trigger_source", sa.String(30), server_default="legacy")):
        op.add_column("rehearsal_enrollments", column)
    db = op.get_bind()
    rows = db.execute(sa.text("""SELECT e.id, e.status, v.sop_id, s.id AS session_id, s.environment,
        c.id AS conversation_id, c.tenant_id, c.contact_id
        FROM rehearsal_enrollments e JOIN sop_versions v ON e.sop_version_id=v.id
        JOIN automation_sessions s ON e.session_id=s.id
        LEFT JOIN conversation_states c ON s.conversation_state_id=c.id ORDER BY e.id""")).mappings().all()
    rounds, active = {}, {}
    for row in rows:
        subject = (f"{row['environment']}:contact:{row['tenant_id']}:{row['contact_id']}" if row['contact_id'] else
                   f"{row['environment']}:conversation:{row['tenant_id']}:{row['conversation_id']}" if row['conversation_id'] else
                   f"{row['environment']}:session:{row['session_id']}")
        key = (row['sop_id'], subject)
        rounds[key] = rounds.get(key, 0) + 1
        db.execute(sa.text("UPDATE rehearsal_enrollments SET sop_id=:sop,subject_key=:subject,round_number=:number WHERE id=:id"),
                   {"sop": row['sop_id'], "subject": subject, "number": rounds[key], "id": row['id']})
        if row['status'] == 'active':
            if key in active:
                db.execute(sa.text("UPDATE rehearsal_enrollments SET status='cancelled' WHERE id=:id"), {"id": active[key]})
                db.execute(sa.text("UPDATE rehearsal_jobs SET status='cancelled',reason='duplicate_round_migration' WHERE enrollment_id=:id AND status IN ('scheduled','waiting_dependency')"), {"id": active[key]})
            active[key] = row['id']
    with op.batch_alter_table("rehearsal_enrollments", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.drop_constraint("uq_rehearsal_enrollments_session_id", type_="unique")
        batch.alter_column("sop_id", nullable=False)
        batch.alter_column("subject_key", nullable=False)
        batch.alter_column("round_number", nullable=False)
        batch.alter_column("trigger_source", nullable=False, server_default=None)
        batch.create_foreign_key("fk_enrollment_sop", "sop_definitions", ["sop_id"], ["id"])
        batch.create_unique_constraint("uq_sop_subject_round", ["sop_id", "subject_key", "round_number"])
        batch.create_unique_constraint("uq_sop_subject_request", ["sop_id", "subject_key", "request_key"])
    op.create_index("uq_sop_active_subject", "rehearsal_enrollments", ["sop_id", "subject_key"], unique=True, sqlite_where=sa.text("status = 'active'"))


def downgrade():
    raise RuntimeError("SOP round history cannot be downgraded without losing reenrollments; restore the backup instead")
