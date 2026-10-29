"""
Create a Super Admin account for VoltGrid.

Admin accounts are never created through the public /register form — only
through this script — so a stranger can't sign up as an admin. Run this once
to create your first admin, then log in normally at /auth.

Usage:
    python create_admin.py
"""
import getpass
import re
import sys

from app import app, db, User


def main():
    print("=== VoltGrid — Create Super Admin ===\n")

    username = input("Admin username: ").strip()
    email = input("Admin email: ").strip().lower()

    if not username or not email:
        print("Username and email are required.")
        sys.exit(1)

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        print("That doesn't look like a valid email address.")
        sys.exit(1)

    password = getpass.getpass("Admin password (min 6 chars): ")
    if len(password) < 6:
        print("Password must be at least 6 characters.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)

    with app.app_context():
        existing = User.query.filter(
            (User.username == username) | (User.email == email)
        ).first()
        if existing:
            print(f"A user with that username or email already exists (role: {existing.role}).")
            sys.exit(1)

        admin = User(
            username=username,
            email=email,
            role="admin",
            email_verified=True,       # admins skip the email-verification flow
            kyc_status="Not Required",
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

    print(f"\nSuper Admin '{username}' created successfully.")
    print("Log in at /auth with this username and password.")


if __name__ == "__main__":
    main()
