"""
Создание первого администратора.
Запуск: python scripts/create_admin.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, create_tables
from app.auth import create_user
from app.models import User

create_tables()

username = input("Логин: ").strip()
password = input("Пароль: ").strip()

if not username or not password:
    print("Логин и пароль не могут быть пустыми")
    sys.exit(1)

db = SessionLocal()
existing = db.query(User).filter(User.username == username).first()
if existing:
    print(f"Пользователь «{username}» уже существует")
    sys.exit(1)

user = create_user(db, username, password)
print(f"Пользователь «{user.username}» создан (id={user.id})")
db.close()
