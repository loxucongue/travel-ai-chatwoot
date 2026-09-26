"""offline evaluation backend

Revision ID: d8e4f2a619ab
Revises: c4f7a8b2d901
"""
from alembic import op
import sqlalchemy as sa


revision = "d8e4f2a619ab"
down_revision = "c4f7a8b2d901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("knowledge_versions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("version_key", sa.String(80), nullable=False, unique=True), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_summary", sa.JSON(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_knowledge_versions_content_hash", "knowledge_versions", ["content_hash"])
    op.create_table("route_branches",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("knowledge_version_id", sa.Integer(), sa.ForeignKey("knowledge_versions.id"), nullable=False),
        sa.Column("branch_key", sa.String(60), nullable=False), sa.Column("name", sa.String(160), nullable=False), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("required_slots", sa.JSON(), nullable=False), sa.Column("complete", sa.Boolean(), nullable=False), sa.Column("priority", sa.Integer(), nullable=False),
        sa.UniqueConstraint("knowledge_version_id", "branch_key"))
    op.create_table("route_nodes",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("route_branch_id", sa.Integer(), sa.ForeignKey("route_branches.id"), nullable=False),
        sa.Column("node_key", sa.String(80), nullable=False), sa.Column("node_type", sa.String(40), nullable=False), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("required_slots", sa.JSON(), nullable=False), sa.Column("evidence_refs", sa.JSON(), nullable=False), sa.Column("asset_keys", sa.JSON(), nullable=False),
        sa.Column("missing_content", sa.Boolean(), nullable=False), sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("route_branch_id", "node_key"))
    op.create_table("material_assets",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("knowledge_version_id", sa.Integer(), sa.ForeignKey("knowledge_versions.id"), nullable=False),
        sa.Column("asset_key", sa.String(120), nullable=False), sa.Column("source_path", sa.String(700), nullable=False), sa.Column("display_name", sa.String(300), nullable=False),
        sa.Column("media_type", sa.String(40), nullable=False), sa.Column("usage", sa.String(200), nullable=False), sa.Column("file_hash", sa.String(64)),
        sa.Column("available", sa.Boolean(), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("knowledge_version_id", "asset_key"))
    op.create_table("evaluation_datasets",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("knowledge_version_id", sa.Integer(), sa.ForeignKey("knowledge_versions.id"), nullable=False), sa.Column("name", sa.String(200), nullable=False),
        sa.Column("filter_version", sa.String(80), nullable=False), sa.Column("filter_config", sa.JSON(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False), sa.Column("snapshot_at", sa.String(40), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_evaluation_datasets_status", "evaluation_datasets", ["status"])
    op.create_table("evaluation_cases",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("evaluation_datasets.id"), nullable=False),
        sa.Column("conversation_state_id", sa.Integer(), sa.ForeignKey("conversation_states.id"), nullable=False), sa.Column("case_key", sa.String(100), nullable=False),
        sa.Column("target_message_ids", sa.JSON(), nullable=False), sa.Column("customer_text", sa.Text(), nullable=False), sa.Column("context_messages", sa.JSON(), nullable=False),
        sa.Column("reference_answer", sa.Text(), nullable=False), sa.Column("expected_branch", sa.String(60)), sa.Column("expected_handoff", sa.Boolean()),
        sa.Column("created_at", sa.String(40), nullable=False), sa.UniqueConstraint("dataset_id", "case_key"))
    op.create_index("ix_evaluation_cases_dataset", "evaluation_cases", ["dataset_id", "id"])
    op.create_table("evaluation_runs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("evaluation_datasets.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True), sa.Column("model", sa.String(120), nullable=False), sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("total_cases", sa.Integer(), nullable=False), sa.Column("completed_cases", sa.Integer(), nullable=False),
        sa.Column("failed_cases", sa.Integer(), nullable=False), sa.Column("metrics", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(120)),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40)), sa.Column("completed_at", sa.String(40)))
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_table("evaluation_results",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("run_id", sa.Integer(), sa.ForeignKey("evaluation_runs.id"), nullable=False),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("evaluation_cases.id"), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("action", sa.String(30)), sa.Column("branch", sa.String(60)), sa.Column("intent", sa.String(60)), sa.Column("reply", sa.Text()),
        sa.Column("slots", sa.JSON(), nullable=False), sa.Column("missing_slots", sa.JSON(), nullable=False), sa.Column("handoff_reason", sa.Text()),
        sa.Column("evidence_refs", sa.JSON(), nullable=False), sa.Column("safety_flags", sa.JSON(), nullable=False), sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("automatic_scores", sa.JSON(), nullable=False), sa.Column("review", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(120)),
        sa.Column("created_at", sa.String(40), nullable=False), sa.Column("completed_at", sa.String(40)), sa.UniqueConstraint("run_id", "case_id"))
    op.create_index("ix_evaluation_results_run", "evaluation_results", ["run_id", "status"])
    op.create_table("model_call_logs",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("evaluation_result_id", sa.Integer(), sa.ForeignKey("evaluation_results.id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("model", sa.String(120), nullable=False), sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False), sa.Column("duration_ms", sa.Integer(), nullable=False), sa.Column("input_tokens", sa.Integer()), sa.Column("output_tokens", sa.Integer()),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("error_code", sa.String(120)), sa.Column("response_meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False))
    op.create_index("ix_model_call_logs_result", "model_call_logs", ["evaluation_result_id", "created_at"])
    op.create_index("ix_model_call_logs_request_hash", "model_call_logs", ["request_hash"])


def downgrade() -> None:
    for table in ["model_call_logs", "evaluation_results", "evaluation_runs", "evaluation_cases", "evaluation_datasets", "material_assets", "route_nodes", "route_branches", "knowledge_versions"]:
        op.drop_table(table)
