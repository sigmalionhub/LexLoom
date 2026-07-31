import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://lexloom_app:app@localhost:5432/lexloom")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "ci")
