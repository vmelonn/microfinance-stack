"""
Login issues a JWT after verifying a password locally -- no ISO 8583, no
switch, no HSM involved, since this is app-level auth, not a card
transaction. set_password exists only to bootstrap the four users who
existed before password_hash was added to the schema (see the migration
note in ledger/db.py) -- it deliberately only works once, for accounts
that have never had a real password, so it can't be used as a password
reset backdoor for an account that already has one. A real reset flow
would need actual identity verification (email/SMS OTP, or similar) --
CNIC alone is not that, and this project doesn't attempt to build that here.
"""

from fastapi import APIRouter, Request, HTTPException

from api.schemas import LoginRequest, TokenResponse, SetPasswordRequest
from ledger.db import get_connection
from auth.passwords import hash_password, verify_password
from auth.tokens import create_token

router = APIRouter()

ACCESS_TOKEN_LIFETIME_SECONDS = 3600


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request):
    conn = get_connection(request.app.state.ledger_db_path)
    row = conn.execute(
        "SELECT user_id, password_hash FROM users WHERE cnic = ?", (body.cnic,)
    ).fetchone()
    conn.close()

    if row is None or not verify_password(body.password, row[1]):
        # Deliberately the same error for "no such user" and "wrong password" --
        # distinguishing them lets an attacker enumerate valid CNICs.
        raise HTTPException(status_code=401, detail="Invalid CNIC or password.")

    user_id = row[0]
    token = create_token({"sub": user_id}, secret=request.app.state.jwt_secret,
                          expires_in_seconds=ACCESS_TOKEN_LIFETIME_SECONDS)
    return TokenResponse(access_token=token, expires_in_seconds=ACCESS_TOKEN_LIFETIME_SECONDS)


@router.post("/auth/set-password")
def set_password(body: SetPasswordRequest, request: Request):
    conn = get_connection(request.app.state.ledger_db_path)
    row = conn.execute("SELECT user_id, password_hash FROM users WHERE cnic = ?", (body.cnic,)).fetchone()

    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="No user registered with this CNIC.")

    user_id, current_hash = row
    if current_hash != "":
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="This account already has a password set. This endpoint only bootstraps accounts that never had one."
        )

    with conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (hash_password(body.new_password), user_id),
        )
    conn.close()
    return {"status": "success", "message": "Password set. You can now log in with POST /auth/login."}
