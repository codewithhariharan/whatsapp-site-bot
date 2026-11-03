"""Make or repair a dashboard admin from the command line.

The dashboard has no sign-up, so the first admin has to come from here. Run it
inside the api container, where the database is reachable:

    docker compose exec api python manage_dashboard.py add-admin you@example.com
    docker compose exec api python manage_dashboard.py reset you@example.com
    docker compose exec api python manage_dashboard.py list

The password is typed at a hidden prompt, never passed as an argument, so it
stays out of shell history and the process list. Everyone else is added from
the dashboard's Users tab.
"""

import argparse
import getpass
import sys

import dashboard_auth as auth


def _ask_password() -> str:
    while True:
        first = getpass.getpass("New password: ")
        problem = auth._password_problem(first)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Again: ") != first:
            print("They don't match. Try again.")
            continue
        return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-admin", help="create an admin account")
    add.add_argument("email")
    add.add_argument("--name", default="")
    reset = sub.add_parser("reset", help="set a new password and make the account an enabled admin")
    reset.add_argument("email")
    sub.add_parser("list", help="list accounts")
    args = parser.parse_args(argv)

    if args.command == "list":
        for u in auth.list_users():
            flags = [f for f, on in (("admin", u["is_admin"]), ("disabled", u["disabled"]),
                                     ("must change password", u["must_change_password"])) if on]
            print(f"{u['email']:40} {', '.join(flags)}")
        return 0

    if args.command == "add-admin":
        if auth.get_user_by_email(args.email):
            print(f"{args.email} already has an account. Use `reset` instead.")
            return 1
        auth.create_user(args.email, _ask_password(), args.name, is_admin=True,
                         must_change=False, created_by="manage_dashboard.py")
        print(f"Admin {args.email} created.")
        return 0

    # reset: the way back in if the only admin forgets their password.
    user = auth.get_user_by_email(args.email)
    if not user:
        print(f"No account for {args.email}.")
        return 1
    auth.set_password(user["id"], _ask_password(), must_change=False)
    auth.db._execute("UPDATE dashboard_users SET is_admin = TRUE, disabled = FALSE "
                     "WHERE id = %s", (user["id"],))
    auth.end_sessions(user["id"])
    print(f"{args.email} reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
