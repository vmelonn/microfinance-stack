from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request):
    """
    Liveness plus a genuine statement about the path to the switch.

    switch_connected is answered by the TRANSPORT, not by poking at a socket
    directly, because what "connected" means differs between the two:

      direct -- is our own TCP connection up?
      ace    -- does an echo test (MTI 0800) complete through ACE and out to
                the switch? A reachable ACE with a dead switch connection is
                NOT healthy, and only a real round trip reveals that.

    A check that only proved this process was running would report healthy
    while every transaction failed.
    """
    transport = request.app.state.transport
    return {
        "status": "ok",
        "transport": type(transport).__name__,
        "switch_connected": transport.is_connected(),
    }
