from fastapi import APIRouter, Request, HTTPException
import sqlite3
import uuid
from api.schemas import UserRegistrationRequest, UserResponse
from ledger.db import get_connection
from auth.passwords import hash_password

router = APIRouter()

@router.post("/users/register", response_model=UserResponse)
def register_user(body: UserRegistrationRequest, request: Request):
    state = request.app.state
    
    # 1. Generate unique, production-grade IDs
    new_user_id = f"usr_{uuid.uuid4().hex[:8]}"
    new_account_id = f"acc_{uuid.uuid4().hex[:8]}"
    password_hash = hash_password(body.password)
    
    conn = get_connection(state.ledger_db_path)
    try:
        with conn:
            # 2. Insert the Identity
            conn.execute(
                "INSERT INTO users (user_id, full_name, cnic, password_hash) VALUES (?, ?, ?, ?)",
                (new_user_id, body.full_name, body.cnic, password_hash)
            )
            
            # 3. Create their Default Wallet Account
            conn.execute(
                "INSERT INTO accounts (account_id, user_id, type) VALUES (?, ?, ?)",
                (new_account_id, new_user_id, "checking")
            )
            
            # 4. Link their Physical Card to that Account
            conn.execute(
                "INSERT INTO cards (card_number, account_id) VALUES (?, ?)",
                (body.bind_card_number, new_account_id)
            )
            
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=400, detail="User with this CNIC or Card already exists.")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
        
    return UserResponse(
        status="success",
        user_id=new_user_id,
        account_id=new_account_id,
        message="User registered, wallet created, and card successfully bound."
    )