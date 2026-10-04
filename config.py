"""Global config: Binance-only."""
import os
from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")
PORT: int = int(os.getenv("PORT", "8765"))

# Binance — единственная площадка.
LEAD_EXCHANGE: str = "binance"
