"""PyInstaller entry point; the database remains outside the EXE bundle."""
from railmail.gui import main
import sys

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from frozen_self_test import run
        sys.exit(run(sys.argv[2]))
    else:
        main()
