import sys
from importlib import import_module
from pathlib import Path

from waitress import serve

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

application = import_module('SBC_PtAttendance.wsgi').application


if __name__ == '__main__':
    serve(
        application,
        host='127.0.0.1',
        port=8000,
        trusted_proxy='127.0.0.1',
        trusted_proxy_count=1,
        trusted_proxy_headers={'x-forwarded-for', 'x-forwarded-proto'},
    )
