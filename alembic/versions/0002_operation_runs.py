"""operation runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('operation_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(), nullable=False),
    sa.Column('command', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('SUCCESS', 'FAILED', name='runstatus', native_enum=False), nullable=False),
    sa.Column('started_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.Column('summary_json', sa.JSON(), nullable=True),
    sa.Column('error_summary', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('operation_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_operation_runs_public_id'), ['public_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('operation_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_operation_runs_public_id'))

    op.drop_table('operation_runs')
