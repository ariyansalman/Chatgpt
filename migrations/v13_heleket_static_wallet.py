"""Add Heleket Static Wallet tables and seed gateway config. Idempotent."""
from database.db import engine
from database.models import Base, PaymentGatewayConfig
from database import get_db_session

def upgrade():
    Base.metadata.create_all(engine)
    with get_db_session() as s:
        if not s.query(PaymentGatewayConfig).filter_by(gateway="heleket").first():
            s.add(PaymentGatewayConfig(gateway="heleket", is_enabled=False))
    print("[OK] Heleket Static Wallet schema/config ready")

if __name__ == "__main__": upgrade()
