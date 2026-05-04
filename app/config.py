import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
BITRIX_MYSQL_URL: str | None = os.getenv("BITRIX_MYSQL_URL")
