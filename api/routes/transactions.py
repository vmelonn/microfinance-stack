"""
The purchase endpoint -- this is Layer 5's actual job description in code.

It doesn't implement anything new. It calls, in order:
  1. The idempotency cache (has this exact intent already been processed?)
  2. Layer 4 (security) -- encrypt the PIN into a proper block
  3. Layer 1 (message layer, via field-building here) -- assemble the DE dict
  4. Layer 3 (correlation) -- send_and_wait(), which itself relies on Layer 2

...and translates whatever comes back into a plain JSON response.
"""
import hashlib
from fastapi import APIRouter, Request, HTTPException, Depends
import secrets
import time

from api.schemas import PurchaseRequest, PurchaseResponse, TransferRequest, TransferResponse
from correlation.tracker import TransactionTimeout
from iso8583.parser import DE39_RESPONSE_CODES
from ledger.db import get_connection
from ledger.service import record_purchase
from auth.dependencies import get_current_user

router = APIRouter()

def resolve_account(conn, identifier: str) -> str | None:
    """
    Translates a physical card number (or direct account ID) into the real Ledger Account ID.
    """
    # 1. Check if the user swiped a registered physical card
    cur = conn.execute("SELECT account_id FROM cards WHERE card_number = ?", (identifier,))
    row = cur.fetchone()
    if row:
        return row[0]
        
    # 2. Check if they provided a direct account ID (like a wallet-to-wallet transfer)
    cur = conn.execute("SELECT account_id FROM accounts WHERE account_id = ?", (identifier,))
    row = cur.fetchone()
    if row:
        return row[0]
        
    # Not found in the system at all
    return None

def _assert_owns_account(conn, account_id: str, user_id: str):
    """
    Authentication alone (a valid token) only proves WHO is calling.
    This is the authorization half: proves the caller actually owns the
    account they're trying to move money out of. Without this, a valid
    token for user A could still debit user B's card, as long as A knows
    B's card number -- exactly the gap this feature exists to close.
    """
    row = conn.execute("SELECT user_id FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    if row is None or row[0] != user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to use this account.")

def _amount_to_minor_units(amount: float) -> str:
    cents = round(amount * 100)
    return f"{cents:012d}"

def _generate_rrn() -> str:
    return f"{int(time.time()) % 10**10:010d}{secrets.randbelow(100):02d}"


@router.post("/transactions/purchase", response_model=PurchaseResponse)
def purchase(body: PurchaseRequest, request: Request, user: dict = Depends(get_current_user)):
    state = request.app.state
    
    # Hash for Idempotency security
    request_hash = hashlib.sha256(body.model_dump_json().encode('utf-8')).hexdigest()

    # -- Step 1: Idempotency -- atomic claim, closes the race the old
    # check-then-process-then-store pattern had (see cache/idempotency_store.py)
    outcome = state.idempotency_store.claim(body.idempotency_key, request_hash)
    if outcome.status == "mismatch":
        raise HTTPException(status_code=400, detail="Idempotency key mismatch.")
    if outcome.status == "duplicate":
        return outcome.cached_response
    if outcome.status == "in_progress":
        raise HTTPException(status_code=409, detail="This request is already being processed. Please retry shortly.")
    # outcome.status == "new" -- we won the claim, proceed

    # -- Step 1.5: THE PRE-CHECK (Verify identities before proceeding)
    ledger_conn = get_connection(state.ledger_db_path)
    
    # Lookup sender's card
    sender_acc = resolve_account(ledger_conn, body.card_number)
    
    # Lookup the merchant (using our hardcoded demo identifier)
    merchant_identifier = "merchant:demo"
    merchant_acc = resolve_account(ledger_conn, merchant_identifier)

    # Block the transaction immediately if either party is unregistered
    if not sender_acc:
        ledger_conn.close()
        raise HTTPException(status_code=404, detail="Sender card is not registered in the system.")
    if not merchant_acc:
        ledger_conn.close()
        raise HTTPException(status_code=404, detail=f"Merchant '{merchant_identifier}' is not registered. Please register the merchant first.")

    # -- Step 1.6: Ownership check -- the token's user must actually own this card
    _assert_owns_account(ledger_conn, sender_acc, user["user_id"])
    ledger_conn.close()

    # -- Step 2: Risk layer 
    amount_cents = round(body.amount * 100)
    risk_decision = state.risk_engine.evaluate(
        card_number=body.card_number,
        amount_cents=amount_cents,
        entry_mode=body.entry_mode, 
    )
    if risk_decision.outcome in ("decline", "review"):
        result = PurchaseResponse(status=risk_decision.outcome, reason="; ".join(risk_decision.reasons))
        state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
        return result

    # -- Step 3: Security layer
    ksn, encrypted_pin_block = state.hsm.encrypt_pin_block(body.pin, body.card_number)

    # -- Step 4+5: Send to the switch, via whichever transport is configured.
    # Under ISO8583_TRANSPORT=direct this builds the message and correlates
    # by STAN locally; under =ace it becomes a SOAP call and ACE does all of
    # that. This code cannot tell the difference, which is the point.
    rrn = _generate_rrn()
    auth = state.transport.authorize(
        pan=body.card_number,
        processing_code="000000",          # 00 = purchase
        amount_minor=_amount_to_minor_units(body.amount),
        entry_mode=body.entry_mode,
        rrn=rrn,
        currency_code="840",
        pin_block=encrypted_pin_block,
        ksn=ksn,
    )

    if auth.outcome == "unknown":
        # THE CASE THAT DID NOT EXIST BEFORE A TIMEOUT WAS MODELLED HONESTLY.
        # The switch may have approved and debited the cardholder, and only
        # the response was lost. Recording a ledger posting would invent a
        # transaction that may never have happened; recording nothing and
        # reporting "declined" would hide one that did.
        #
        # So: no posting, and the caller is told explicitly. Daily
        # reconciliation (ops/reconciliation.py) is the backstop that catches
        # it against the switch's own settlement file.
        result = PurchaseResponse(
            status="unknown",
            reason=(auth.response_text or "Switch outcome could not be determined.")
                   + " Not recorded in the ledger; awaiting reconciliation.",
            rrn=rrn,
        )
        state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
        return result

    if auth.outcome == "approved":
        confirmed_rrn = auth.rrn or rrn

        # -- Step 6: Ledger layer (Using the real, mapped Database IDs)
        ledger_conn = get_connection(state.ledger_db_path)
        ledger_result = record_purchase(
            ledger_conn,
            rrn=confirmed_rrn,
            debit_account=sender_acc,      # Securely mapped sender wallet ID
            credit_account=merchant_acc,   # Securely mapped merchant wallet ID
            amount_cents=amount_cents,
        )
        ledger_conn.close()

        result = PurchaseResponse(
            status="approved",
            reason=auth.response_text or "Approved",
            authorization_id=auth.authorization_id,
            stan=auth.stan,
            rrn=confirmed_rrn,
            ledger_status=ledger_result["status"],
        )
    else:
        result = PurchaseResponse(
            status="declined",
            reason=auth.response_text or "Declined by Host",
            stan=auth.stan,
            rrn=rrn,
        )

    state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
    return result
# --- api/routes/transactions.py ---

from ledger.service import get_balance, is_balanced

@router.get("/ledger/balance/{account_id}")
def check_balance(account_id: str, request: Request, user: dict = Depends(get_current_user)):
    """
    Check the balance. You can input a raw Wallet ID (acc_123), 
    a physical card number (4111...), or a merchant identifier (merchant:demo).
    Requires the authenticated caller to own the resolved account.
    """
    from ledger.db import get_connection
    from fastapi import HTTPException
    
    state = request.app.state
    conn = get_connection(state.ledger_db_path)
    
    try:
        # 1. Translate whatever the user typed into the true Wallet ID
        true_wallet_id = resolve_account(conn, account_id)
        
        if not true_wallet_id:
            raise HTTPException(status_code=404, detail="Account or Card not found in the system.")

        # 1.5 Ownership check -- you can only check your own balance
        _assert_owns_account(conn, true_wallet_id, user["user_id"])

        # 2. Query the ledger using the true Wallet ID
        cur = conn.execute(
            """
            SELECT 
                SUM(CASE WHEN entry_type = 'credit' THEN amount_cents ELSE 0 END) -
                SUM(CASE WHEN entry_type = 'debit' THEN amount_cents ELSE 0 END)
            FROM ledger_entries
            WHERE account_id = ?
            """,
            (true_wallet_id,)
        )
        row = cur.fetchone()
        balance_cents = row[0] if row[0] is not None else 0
        
    finally:
        conn.close()

    return {
        "provided_identifier": account_id,
        "resolved_wallet_id": true_wallet_id,
        "balance_usd": balance_cents / 100.0
    }

@router.post("/transactions/transfer", response_model=TransferResponse)
def transfer(body: TransferRequest, request: Request, user: dict = Depends(get_current_user)):
    state = request.app.state
    
    request_hash = hashlib.sha256(body.model_dump_json().encode('utf-8')).hexdigest()

    # -- Step 1: Idempotency -- same atomic claim as purchase()
    outcome = state.idempotency_store.claim(body.idempotency_key, request_hash)
    if outcome.status == "mismatch":
        raise HTTPException(status_code=400, detail="Idempotency key mismatch.")
    if outcome.status == "duplicate":
        return outcome.cached_response
    if outcome.status == "in_progress":
        raise HTTPException(status_code=409, detail="This request is already being processed. Please retry shortly.")

    # -- Step 1.5: THE PRE-CHECK (Verify identities before proceeding)
    ledger_conn = get_connection(state.ledger_db_path)
    sender_acc = resolve_account(ledger_conn, body.sender_card_number)
    recipient_acc = resolve_account(ledger_conn, body.recipient_account)

    if not sender_acc:
        ledger_conn.close()
        raise HTTPException(status_code=404, detail="Sender card is not registered.")
    if not recipient_acc:
        ledger_conn.close()
        raise HTTPException(status_code=404, detail="Recipient is not registered.")

    # -- Step 1.6: Ownership check -- can't transfer FROM a card that isn't yours
    _assert_owns_account(ledger_conn, sender_acc, user["user_id"])
    ledger_conn.close()

    # -- Step 2: Risk layer 
    amount_cents = round(body.amount * 100)
    risk_decision = state.risk_engine.evaluate(
        card_number=body.sender_card_number,
        amount_cents=amount_cents,
        entry_mode="01", 
    )
    if risk_decision.outcome in ("decline", "review"):
        result = TransferResponse(status=risk_decision.outcome, reason="; ".join(risk_decision.reasons))
        state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
        return result

    # -- Step 3: Security layer
    ksn, encrypted_pin_block = state.hsm.encrypt_pin_block(body.sender_pin, body.sender_card_number)

    # -- Step 4+5: Same transport indirection as purchase(). DE 3 = 400000
    # (transfer between accounts) and DE 103 carries the credit side.
    rrn = _generate_rrn()
    auth = state.transport.authorize(
        pan=body.sender_card_number,
        processing_code="400000",
        amount_minor=_amount_to_minor_units(body.amount),
        entry_mode="01",
        rrn=rrn,
        currency_code="840",
        pin_block=encrypted_pin_block,
        ksn=ksn,
        account_id_2=recipient_acc,   # the real wallet ID, not a placeholder
    )

    if auth.outcome == "unknown":
        result = TransferResponse(
            status="unknown",
            reason=(auth.response_text or "Switch outcome could not be determined.")
                   + " Not recorded in the ledger; awaiting reconciliation.",
            rrn=rrn,
        )
        state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
        return result

    if auth.outcome == "approved":
        confirmed_rrn = auth.rrn or rrn

        # -- Step 6: Ledger layer (Using the real, mapped Database IDs)
        ledger_conn = get_connection(state.ledger_db_path)
        ledger_result = record_purchase(
            ledger_conn,
            rrn=confirmed_rrn,
            debit_account=sender_acc,
            credit_account=recipient_acc,
            amount_cents=amount_cents,
        )
        ledger_conn.close()

        result = TransferResponse(
            status="approved",
            reason=auth.response_text or "Approved",
            authorization_id=auth.authorization_id,
            stan=auth.stan,
            rrn=confirmed_rrn,
            ledger_status=ledger_result["status"],
        )
    else:
        result = TransferResponse(
            status="declined",
            reason=auth.response_text or "Declined by Host",
            stan=auth.stan,
            rrn=rrn,
        )

    state.idempotency_store.store_response(body.idempotency_key, result.model_dump())
    return result

@router.post("/ledger/reset")
def reset_ledger(request: Request, user: dict = Depends(get_current_user)):
    """
    DANGER: This completely wipes the ledger database.
    Perfect for sandbox testing, catastrophic in production!

    Requires a valid token, but NOT an admin role -- this project doesn't
    have a role system yet, so any authenticated user can currently wipe
    the entire ledger, not just their own data. Real ADMIN-only gating is
    a follow-up, not solved by this change; auth here only rules out
    completely anonymous callers.
    """
    from ledger.db import get_connection
    state = request.app.state
    
    conn = get_connection(state.ledger_db_path)
    try:
        with conn:
            # 1. Delete all rows from both tables
            conn.execute("DELETE FROM ledger_entries")
            conn.execute("DELETE FROM transactions")
            
            # 2. Reset the SQLite auto-increment ID counter back to 0
            # (Fails silently if sqlite_sequence doesn't exist yet, which is fine)
            try:
                conn.execute("DELETE FROM sqlite_sequence WHERE name='ledger_entries'")
            except Exception:
                pass
    finally:
        conn.close()
    
    # 3. Clear the idempotency store (works for either backend, in-memory or Redis)
    # This allows you to reuse "txn-987654321" again
    state.idempotency_store.clear_all()

    return {
        "status": "success", 
        "message": "The ledger and idempotency cache have been completely wiped."
    }