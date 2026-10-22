"""add mark_hesitation table

Revision ID: c3e1f5a7b9d2
Revises: a0799bd18964
Create Date: 2026-10-22 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3e1f5a7b9d2'
down_revision: Union[str, Sequence[str], None] = 'a0799bd18964'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Purely additive: a new table nothing else references."""
    op.create_table('mark_hesitation',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('round_id', sa.Integer(), nullable=False),
    sa.Column('entry_id', sa.Integer(), nullable=False),
    sa.Column('judge_person_id', sa.Integer(), nullable=False),
    sa.Column('dance', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['entry_id'], ['entry.id'], ),
    sa.ForeignKeyConstraint(['judge_person_id'], ['person.id'], ),
    sa.ForeignKeyConstraint(['round_id'], ['round.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('round_id', 'entry_id', 'judge_person_id', 'dance')
    )
    op.create_index(op.f('ix_mark_hesitation_entry_id'), 'mark_hesitation', ['entry_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_mark_hesitation_entry_id'), table_name='mark_hesitation')
    op.drop_table('mark_hesitation')
