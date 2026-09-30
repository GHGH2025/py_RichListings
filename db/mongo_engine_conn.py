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
    # Burst guard (Rich 29.09, RDS/Mongo connections alarm 27/30): the Mongo box is shared with
    # external clients (Compass), so cap THIS box's pool and release idle sockets fast, so a burst
    # of concurrent pipeline work can't monopolise the shared ~30-connection budget. Tunable via env.
    kwargs["maxPoolSize"] = int(os.getenv("MONGO_MAX_POOL_SIZE", "20"))
    kwargs["maxIdleTimeMS"] = int(os.getenv("MONGO_MAX_IDLE_MS", "60000"))
    if TLS:
        kwargs.update({
            "tls": True,
            "tlsAllowInvalidCertificates": TLS_ALLOW_INVALID_CERTS,
            "tlsAllowInvalidHostnames": TLS_ALLOW_INVALID_HOSTNAMES,
        })
    # Connect using URI (db name can be in the URI path)
    connect(host=MONGO_URI, **kwargs)
