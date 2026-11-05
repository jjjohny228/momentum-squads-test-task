"""initial schema

Revision ID: 1b430416b9ed
Revises: 
Create Date: 2026-11-05 21:14:27.404748
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '1b430416b9ed'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('kb_articles',
    sa.Column('slug', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.String(length=64)), nullable=False),
    sa.Column('language', sa.String(length=8), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('search_vector', postgresql.TSVECTOR(), sa.Computed("to_tsvector('simple', title || ' ' || body)", persisted=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('slug', name=op.f('pk_kb_articles'))
    )
    op.create_index('ix_kb_articles_search_vector', 'kb_articles', ['search_vector'], unique=False, postgresql_using='gin')
    op.create_table('provider_calls',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('bucket', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_calls'))
    )
    op.create_index('ix_provider_calls_bucket_created_at', 'provider_calls', ['bucket', 'created_at'], unique=False)
    op.create_table('tickets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('event_id', sa.String(length=255), nullable=False),
    sa.Column('external_ticket_id', sa.String(length=255), nullable=False),
    sa.Column('customer_email', sa.String(length=320), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('reply_url', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('draft', sa.Text(), nullable=True),
    sa.Column('draft_source', sa.String(length=16), nullable=True),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('needs_manual_reply', sa.Boolean(), nullable=False),
    sa.Column('final_reply', sa.Text(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tickets')),
    sa.UniqueConstraint('event_id', name=op.f('uq_tickets_event_id'))
    )
    op.create_index(op.f('ix_tickets_external_ticket_id'), 'tickets', ['external_ticket_id'], unique=False)
    op.create_index('ix_tickets_status_created_at', 'tickets', ['status', 'created_at'], unique=False)
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('ticket_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_jobs_ticket_id_tickets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index('ix_jobs_claimable', 'jobs', ['run_after'], unique=False, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_index(op.f('ix_jobs_ticket_id'), 'jobs', ['ticket_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_jobs_ticket_id'), table_name='jobs')
    op.drop_index('ix_jobs_claimable', table_name='jobs', postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_table('jobs')
    op.drop_index('ix_tickets_status_created_at', table_name='tickets')
    op.drop_index(op.f('ix_tickets_external_ticket_id'), table_name='tickets')
    op.drop_table('tickets')
    op.drop_index('ix_provider_calls_bucket_created_at', table_name='provider_calls')
    op.drop_table('provider_calls')
    op.drop_index('ix_kb_articles_search_vector', table_name='kb_articles', postgresql_using='gin')
    op.drop_table('kb_articles')
