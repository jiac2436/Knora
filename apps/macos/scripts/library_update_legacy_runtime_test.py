"""Existing full-workflow acceptance with the current private startup contract."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_domain_full_runtime_test as regression
from library_update_runtime_test import ExternalHost

if __name__ == '__main__':
    regression.Host = ExternalHost
    regression.main()
