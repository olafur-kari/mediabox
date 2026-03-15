#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import click
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import User
from app.auth import add_user

DATABASE_URL = "sqlite:////data/mediabox.db"
engine = create_engine(DATABASE_URL, echo=False)


def get_session():
    SQLModel.metadata.create_all(engine)
    return Session(engine)


@click.group()
def cli():
    """Mediabox CLI — manage users and configuration."""
    pass


@cli.command("add-user")
@click.argument("username")
@click.argument("password")
@click.option("--admin", is_flag=True, default=False, help="Grant admin privileges")
def cmd_add_user(username: str, password: str, admin: bool):
    """Add a new user to the database."""
    session = get_session()
    try:
        user = add_user(session, username, password, is_admin=admin)
        role = "admin" if user.is_admin else "user"
        click.echo(f"Created {role}: {user.username} (id={user.id})")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        session.close()


@cli.command("change-password")
@click.argument("username")
@click.argument("new_password")
def cmd_change_password(username: str, new_password: str):
    """Change a user's password."""
    from app.auth import hash_password
    session = get_session()
    try:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            click.echo(f"Error: user '{username}' not found", err=True)
            sys.exit(1)
        user.hashed_password = hash_password(new_password)
        session.add(user)
        session.commit()
        click.echo(f"Password updated for: {username}")
    finally:
        session.close()


@cli.command("list-users")
def cmd_list_users():
    """List all users."""
    session = get_session()
    try:
        users = session.exec(select(User)).all()
        if not users:
            click.echo("No users found.")
            return
        click.echo(f"{'ID':<5} {'Username':<20} {'Admin':<8} {'Created'}")
        click.echo("-" * 55)
        for u in users:
            role = "yes" if u.is_admin else "no"
            created = u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "—"
            click.echo(f"{u.id:<5} {u.username:<20} {role:<8} {created}")
    finally:
        session.close()


if __name__ == "__main__":
    cli()
