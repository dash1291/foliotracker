"""account closed_as_of

Revision ID: 7b2e9c4d1a05
Revises: 14d04b64f372
Create Date: 2026-11-13 16:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b2e9c4d1a05'
down_revision: Union[str, Sequence[str], None] = '14d04b64f372'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('closed_as_of', sa.Date(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_column('closed_as_of')
