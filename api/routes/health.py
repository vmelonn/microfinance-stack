from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request):
    connected = request.app.state.client._connected.is_set()
    return {"status": "ok", "switch_connected": connected}
