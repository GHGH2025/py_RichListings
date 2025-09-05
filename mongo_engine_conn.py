# mongo_engine_conn.py
import os
from dotenv import load_dotenv
from mongoengine import connect

load_dotenv()

MONGO_URI  = os.getenv("MONGO_URI")
ALIAS      = os.getenv("MONGO_ALIAS", "default")

# Optional TLS flags (useful if your cert/hostname is non-standard in dev)
def _as_bool(v, default=False):
    return str(v).lower() in ("1","true","yes","y") if v is not None else default

TLS                           = _as_bool(os.getenv("MONGO_TLS"), False)
TLS_ALLOW_INVALID_CERTS       = _as_bool(os.getenv("MONGO_TLS_ALLOW_INVALID_CERTS"), False)
TLS_ALLOW_INVALID_HOSTNAMES   = _as_bool(os.getenv("MONGO_TLS_ALLOW_INVALID_HOSTNAMES"), False)

def init_db(alias: str = ALIAS):
    kwargs = {"alias": alias}
    if TLS:
        kwargs.update({
            "tls": True,
            "tlsAllowInvalidCertificates": TLS_ALLOW_INVALID_CERTS,
            "tlsAllowInvalidHostnames": TLS_ALLOW_INVALID_HOSTNAMES,
        })
    # Connect using URI (db name can be in the URI path)
    connect(host=MONGO_URI, **kwargs)
