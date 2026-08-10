"""
The FastAPI dependency every protected route uses: pulls the Authorization
header, verifies the token, and resolves it to a real, still-existing user.
Route handlers just declare `user = Depends(get_current_user)` and never
touch tokens, headers, or the users table directly.
"""

from fastapi import Request, Header, HTTPException

from auth.tokens import decode_token, TokenError
from ledger.db import get_connection


def get_current_user(request: Request, authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header. Expected: Bearer <token>")

    token = authorization[len("Bearer "):].strip()

    try:
        claims = decode_token(token, secret=request.app.state.jwt_secret)
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user_id = claims.get("sub")
    conn = get_connection(request.app.state.ledger_db_path)
    row = conn.execute("SELECT user_id, full_name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    if row is None:
        # The token is validly signed, but the user it names is gone --
        # treat this the same as any other invalid token, not a 404,
        # since it's still fundamentally an auth failure from the caller's view.
        raise HTTPException(status_code=401, detail="Token refers to a user that no longer exists")

    return {"user_id": row[0], "full_name": row[1]}
