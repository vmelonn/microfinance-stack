"""
App entry point for Layer 5.

On startup, this wires together everything built in Stages 1-4: opens the
connection to a switch (here, our own host simulator, since this is a
learning build with no real switch to point at -- swap SWITCH_HOST/PORT for
a real one and this code doesn't otherwise change), sets up correlation and
security, and stores them on app.state so every request handler can reach
them without any global variables.
"""

import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Ensure the root directory is accessible for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from switch.client import ISO8583Client
from switch.host_simulator import HostSimulator
from switch.transport import build_transport
from correlation.tracker import CorrelationManager
from security.mock_hsm import MockHSM
from ledger.db import init_db
from risk.rules import RiskEngine
from ops.audit_log import AuditLogger
from cache.idempotency_store import InMemoryIdempotencyStore, RedisIdempotencyStore
from cache.velocity_tracker import InMemoryVelocityTracker, RedisVelocityTracker
from security.kms import LocalKeyManagementService
from api.routes import transactions, health, users, auth

SWITCH_HOST = os.environ.get("SWITCH_HOST", "127.0.0.1")
SWITCH_PORT = int(os.environ.get("SWITCH_PORT", "9999"))
RUN_LOCAL_SIMULATOR = os.environ.get("RUN_LOCAL_SIMULATOR", "1") == "1"

# How transactions reach the switch. See switch/transport.py.
#
#   direct -- Stages 1-3 as built: this process owns the codec, the socket,
#             and STAN correlation. The default; needs nothing external.
#   ace    -- SOAP to an IBM ACE integration server, which owns all of it.
#             ACE parses and serializes ISO 8583 from a DFDL schema and holds
#             the TCP connection itself, so none of Stages 1-3 run here.
#
# When ISO8583_TRANSPORT=ace, this process opens no socket to the switch at
# all -- so the local simulator and the ISO8583Client below are skipped
# entirely rather than started and left idle.
ISO8583_TRANSPORT = os.environ.get("ISO8583_TRANSPORT", "direct").lower()
USE_ACE = ISO8583_TRANSPORT == "ace"

# Same pattern as RUN_LOCAL_SIMULATOR: unset by default, so local dev needs
# no Redis at all. Set REDIS_URL to opt into shared, horizontal-scaling-safe
# idempotency and velocity state -- required once you run more than one
# replica, since in-memory state is invisible across separate processes.
REDIS_URL = os.environ.get("REDIS_URL")

# Same opt-in pattern again: unset by default, so local dev gets today's
# ephemeral base_key (fine for a single demo run). Set both to make the
# HSM's key genuinely survive restarts -- see security/kms.py.
HSM_KEY_PERSISTENCE_PATH = os.environ.get("HSM_KEY_PERSISTENCE_PATH")
HSM_MASTER_KEY_HEX = os.environ.get("HSM_MASTER_KEY_HEX")  # 64 hex chars = 32 bytes

_DEV_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-do-not-use-in-production"
JWT_SECRET = os.environ.get("JWT_SECRET", _DEV_DEFAULT_JWT_SECRET)
if JWT_SECRET == _DEV_DEFAULT_JWT_SECRET:
    print("[api.main] WARNING: JWT_SECRET is not set -- using an insecure development "
          "default. Every token issued with this secret is forgeable by anyone who reads "
          "this source file. Set the JWT_SECRET environment variable before deploying anywhere real.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if USE_ACE:
        # ACE owns the codec, the framing, the socket, and STAN correlation,
        # so none of Stages 1-3 start here. Starting a simulator and a client
        # that nothing would ever use would just be two idle threads and a
        # misleading /health answer.
        app.state.simulator = None
        app.state.client = None
        app.state.correlator = None
        app.state.audit_logger = AuditLogger()
        app.state.transport = build_transport()
    else:
        # 1. Start the Host Simulator (Bank Switch)
        if RUN_LOCAL_SIMULATOR:
            app.state.simulator = HostSimulator(host=SWITCH_HOST, port=SWITCH_PORT)
            app.state.simulator.start()
            time.sleep(0.2)

        # 2. Connect the ISO8583 Client to the Switch
        app.state.client = ISO8583Client(
            SWITCH_HOST,
            SWITCH_PORT,
            heartbeat_interval=3,
            audit_logger=AuditLogger()
        )
        app.state.client.connect()

        # 3. Wait for the connection to fully establish
        connect_deadline = time.monotonic() + 10
        while not app.state.client._connected.is_set():
            if time.monotonic() > connect_deadline:
                raise RuntimeError(
                    f"Could not connect to switch at {SWITCH_HOST}:{SWITCH_PORT} within 10s. "
                    "If RUN_LOCAL_SIMULATOR=1, check nothing else is using that port, and check "
                    "whether a firewall prompt is waiting for approval."
                )
            time.sleep(0.05)

        # 4. Wire up all the Microfinance Services
        app.state.audit_logger = app.state.client.audit_logger
        app.state.correlator = CorrelationManager(app.state.client, timeout_seconds=10)
        app.state.transport = build_transport(app.state.client, app.state.correlator)

    if HSM_KEY_PERSISTENCE_PATH and HSM_MASTER_KEY_HEX:
        kms = LocalKeyManagementService(master_key=bytes.fromhex(HSM_MASTER_KEY_HEX))
        app.state.hsm = MockHSM(kms=kms, persisted_key_path=HSM_KEY_PERSISTENCE_PATH)
        print(f"[api.main] HSM base_key persisted at {HSM_KEY_PERSISTENCE_PATH} -- survives restarts.")
    else:
        app.state.hsm = MockHSM()
        print("[api.main] HSM_KEY_PERSISTENCE_PATH/HSM_MASTER_KEY_HEX not set -- HSM base_key is "
              "ephemeral, regenerated every restart. Anything encrypted before a restart cannot be "
              "decrypted after one.")

    if REDIS_URL:
        import redis
        redis_client = redis.Redis.from_url(REDIS_URL)
        redis_client.ping()  # fail fast at startup, not on the first request, if Redis is unreachable
        app.state.redis_client = redis_client
        app.state.idempotency_store = RedisIdempotencyStore(redis_client)
        velocity_tracker = RedisVelocityTracker(redis_client)
        print(f"[api.main] Using Redis for idempotency + velocity state ({REDIS_URL}) -- safe across multiple replicas.")
    else:
        app.state.redis_client = None
        app.state.idempotency_store = InMemoryIdempotencyStore()
        velocity_tracker = InMemoryVelocityTracker()
        print("[api.main] REDIS_URL not set -- using in-memory idempotency + velocity state. "
              "Fine for a single process; set REDIS_URL before running more than one replica.")

    app.state.risk_engine = RiskEngine(velocity_tracker=velocity_tracker)
    app.state.jwt_secret = JWT_SECRET

    # 5. Define Database Path & Auto-Build the Tables
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    app.state.ledger_db_path = os.path.join(BASE_DIR, "ledger.db")
    init_db(app.state.ledger_db_path)

    yield

    # 6. Teardown on shutdown
    app.state.transport.close()
    if app.state.simulator is not None:
        app.state.simulator.stop()


# 7. Build the FastAPI App and Register Routers
app = FastAPI(title="Microfinance Transaction API", lifespan=lifespan)

app.include_router(transactions.router)
app.include_router(users.router) 
app.include_router(auth.router)
app.include_router(health.router)