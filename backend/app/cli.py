import argparse
import getpass

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import Tenant, User
from app.security import hash_password


def create_admin(email: str) -> None:
    Base.metadata.create_all(engine)
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm or len(password) < 8:
        raise SystemExit("Passwords must match and contain at least 8 characters")
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email.lower())):
            raise SystemExit("User already exists")
        if not db.scalar(select(Tenant)):
            db.add(Tenant(name="china2go"))
        db.add(User(email=email.lower(), display_name=email.split("@")[0], password_hash=hash_password(password), role="super_admin"))
        db.commit()
    print(f"Created super admin: {email}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("create-admin")
    command.add_argument("--email", required=True)
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.email)


if __name__ == "__main__":
    main()

