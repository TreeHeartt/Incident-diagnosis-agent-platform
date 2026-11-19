"""Authenticated task platform schema; legacy demo tables are retained."""
from alembic import op
from incident_simulator.database import Base
revision='0001_platform'
down_revision=None
branch_labels=None
depends_on=None
def upgrade(): Base.metadata.create_all(op.get_bind())
def downgrade(): raise RuntimeError('Destructive downgrade unsupported; restore a backup')
