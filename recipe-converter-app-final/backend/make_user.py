"""
make_user.py — generate one APP_USERS entry (email + hashed password).

Usage:
    python make_user.py thomas@wynn.com            # normal user
    python make_user.py chef@wynn.com admin        # reviewer/admin

Copy the printed line into the APP_USERS environment variable (comma-separate
multiple users). The plain password is never stored — only its PBKDF2 hash.
"""
import getpass, sys
from security import hash_password

def main():
    email = sys.argv[1] if len(sys.argv) > 1 else input("email: ")
    is_admin = len(sys.argv) > 2 and sys.argv[2].lower() == "admin"
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("confirm : "):
        print("Passwords do not match."); sys.exit(1)
    line = f"{email.strip().lower()}|{hash_password(pw)}" + ("|admin" if is_admin else "")
    print("\nAdd this to APP_USERS (comma-separate multiple users):\n")
    print(line)

if __name__ == "__main__":
    main()
