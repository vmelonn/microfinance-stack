"""
Request/response shapes for the REST API layer.

Notice what's NOT in here: no MTI, no DE numbers, no STAN, no bitmap. A
client of this API should never need to know any of that -- everything
below this layer is deliberately invisible to them.
"""

from pydantic import BaseModel, Field


class PurchaseRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in whole currency units, e.g. 50.00")
    card_number: str = Field(..., min_length=12, max_length=19)
    pin: str = Field(..., min_length=4, max_length=6)
    idempotency_key: str = Field(..., description="Client-generated ID representing this transaction intent")
    entry_mode: str = Field("05", description="DE 22 style code, e.g. '05' chip, '01' manual key entry")


class PurchaseResponse(BaseModel):
    status: str                  # "approved" | "declined" | "error"
    reason: str | None = None    # human-readable meaning, from DE 39's code table
    authorization_id: str | None = None
    stan: str | None = None
    rrn: str | None = None
    ledger_status: str | None = None   # "recorded" | "already_recorded" | None if not approved

class TransferRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in whole currency units, e.g. 50.00")
    sender_card_number: str = Field(..., min_length=12, max_length=19)
    sender_pin: str = Field(..., min_length=4, max_length=6)
    recipient_account: str = Field(..., min_length=4, description="The account ID to credit")
    idempotency_key: str = Field(..., description="Client-generated ID representing this transaction intent")

class TransferResponse(BaseModel):
    status: str                  # "approved" | "declined" | "error"
    reason: str | None = None    
    authorization_id: str | None = None
    stan: str | None = None
    rrn: str | None = None
    ledger_status: str | None = None

class UserRegistrationRequest(BaseModel):
    full_name: str = Field(..., description="The user's real legal name")
    cnic: str = Field(..., min_length=13, max_length=13, description="13-digit Pakistani CNIC")
    bind_card_number: str = Field(..., min_length=12, description="The test card number to link to this user")
    password: str = Field(..., min_length=8, description="App login password -- NOT the card PIN, a separate secret")

class UserResponse(BaseModel):
    status: str
    user_id: str
    account_id: str
    message: str

class LoginRequest(BaseModel):
    cnic: str = Field(..., min_length=13, max_length=13)
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int

class SetPasswordRequest(BaseModel):
    cnic: str = Field(..., min_length=13, max_length=13)
    new_password: str = Field(..., min_length=8)