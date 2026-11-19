from alembic import op
from incident_simulator.database import Runbook, Evaluation
revision='0002_knowledge_evaluation'
down_revision='0001_platform'
branch_labels=None
depends_on=None
def upgrade():
    Runbook.__table__.create(op.get_bind(),checkfirst=True)
    Evaluation.__table__.create(op.get_bind(),checkfirst=True)
def downgrade(): raise RuntimeError('Restore a backup instead')
