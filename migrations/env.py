from alembic import context
from incident_simulator.database import Base, engine
with engine.connect() as connection:
    context.configure(connection=connection,target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
