# ISO 8583 data element code mappings

Most DEs just hold free-form data (a PAN, an amount, a merchant name) — there's
nothing to "map," the value just is what it is. But a handful of DEs are coded
fields, where the value is drawn from a fixed list of meanings. This document
covers those.

Note: several of these (DE 3, 22, 25, 39, 60 especially) have base-standard
meanings but get extended or reinterpreted per network (Visa, Mastercard,
national switches). Treat the tables below as the common/base definitions —
always confirm against your specific processor's spec before relying on them
in production.

---

## DE 3 — Processing code

6 digits, split into three 2-digit sub-fields: `TT AA AA`

- **Digits 1–2 (TT): Transaction type**

| Code | Meaning |
|------|---------|
| 00 | Purchase |
| 01 | Withdrawal (cash) |
| 02 | Adjustment |
| 09 | Purchase with cashback |
| 10 | Balance inquiry |
| 17 | Payment |
| 18 | Transfer |
| 20 | Refund/deposit |
| 21 | Deposit |
| 22 | Balance inquiry (alt) |
| 28 | Fee collection |
| 30 | Balance inquiry (savings) |
| 40 | Transfer between accounts |
| 50 | Bill payment |
| 90 | Reversal |
| 91 | Reversal, partial |

- **Digits 3–4: Account type, "from"**
- **Digits 5–6: Account type, "to"**

| Code | Meaning |
|------|---------|
| 00 | Default / not specified |
| 10 | Savings account |
| 20 | Checking/current account |
| 30 | Credit card account |
| 40 | Universal/general ledger account |
| 50 | Investment account |

Example: `000000` = purchase, default account, default account. `011000` = cash withdrawal, from savings, to default.

---

## DE 18 — Merchant category code (MCC)

4-digit numeric code identifying the merchant's business type, assigned by ISO 18245. There are thousands of these; a small sample of common ones:

| MCC | Business type |
|-----|---------------|
| 5411 | Grocery stores, supermarkets |
| 5541 | Service stations (fuel) |
| 5812 | Restaurants |
| 5912 | Drug stores, pharmacies |
| 5999 | Miscellaneous retail |
| 6011 | ATM / financial institution cash disbursement |
| 4111 | Local/suburban transit |
| 4900 | Utilities |
| 8011 | Doctors, physicians |
| 8062 | Hospitals |
| 5999 | Misc. retail stores |
| 7011 | Hotels/motels |

Full MCC lists (thousands of codes) are published by the card networks (Visa, Mastercard) — not worth reproducing here in full, but worth knowing the field is a lookup into that published list, not something you'd hardcode a small dict for.

---

## DE 22 — Point of service entry mode

3 digits: `PP CC` — first two digits = PAN entry mode, third = PIN entry capability (varies by version; the first two digits are the ones you'll use most).

| Code | Meaning |
|------|---------|
| 00 | Unknown / unspecified |
| 01 | Manual key entry |
| 02 | Magnetic stripe read |
| 03 | Bar code |
| 04 | OCR |
| 05 | Chip (EMV), CVV can be checked |
| 07 | Contactless chip (EMV) |
| 10 | Merchant has credit card imprinter only |
| 51 | Chip, PIN verified |
| 79 | Chip fallback to magstripe |
| 81 | Contactless magstripe |
| 90 | Magnetic stripe, all track data present |
| 91 | Contactless chip |

---

## DE 25 — Point of service condition code

2 digits, describes the circumstance of the transaction.

| Code | Meaning |
|------|---------|
| 00 | Normal presentment, cardholder present |
| 01 | Cardholder not present (mail/phone/internet) |
| 02 | Unattended terminal |
| 03 | Merchant suspicious of transaction |
| 04 | Cardholder present, card not present (e.g. phone-in) |
| 05 | Cardholder not present, preauthorized |
| 06 | Cardholder present, magnetic stripe read failure |
| 08 | Mail order |
| 59 | Suspicious transaction (variant) |
| 71 | Chip card, chip read failure, fallback |
| 90 | Original transaction |

---

## DE 26 — Point of service PIN capture code

2 digits, indicates the maximum number of PIN characters the terminal can capture.

| Code | Meaning |
|------|---------|
| 00 | Unspecified / unknown |
| 04–12 | Terminal can capture 4–12 digit PIN |

---

## DE 39 — Response code

2 digits (alphanumeric, but almost always numeric in practice). This is the field your app logic actually branches on.

| Code | Meaning |
|------|---------|
| 00 | Approved / completed successfully |
| 01 | Refer to card issuer |
| 02 | Refer to card issuer, special condition |
| 03 | Invalid merchant |
| 04 | Pick up card |
| 05 | Do not honor (generic decline) |
| 06 | Error |
| 07 | Pick up card, special condition |
| 08 | Honor with identification |
| 09 | Request in progress |
| 10 | Approved for partial amount |
| 12 | Invalid transaction |
| 13 | Invalid amount |
| 14 | Invalid card number |
| 15 | No such issuer |
| 17 | Customer cancellation |
| 19 | Re-enter transaction |
| 20 | Invalid response |
| 21 | No action taken |
| 25 | Unable to locate record |
| 30 | Format error |
| 31 | Bank not supported by switch |
| 33 | Expired card, pick up |
| 34 | Suspected fraud, pick up |
| 38 | Allowable PIN tries exceeded, pick up |
| 39 | No credit account |
| 41 | Lost card, pick up |
| 43 | Stolen card, pick up |
| 51 | Insufficient funds |
| 52 | No checking account |
| 53 | No savings account |
| 54 | Expired card |
| 55 | Incorrect PIN |
| 56 | No card record |
| 57 | Transaction not permitted to cardholder |
| 58 | Transaction not permitted to terminal |
| 59 | Suspected fraud |
| 61 | Exceeds withdrawal amount limit |
| 62 | Restricted card |
| 63 | Security violation |
| 65 | Exceeds withdrawal frequency limit |
| 68 | Response received too late |
| 75 | Allowable PIN tries exceeded |
| 76 | Unable to locate previous message |
| 77 | Original amount incorrect |
| 78 | No account |
| 80 | Invalid date |
| 81 | Cryptographic error in PIN |
| 82 | Negative card verification value (CVV) |
| 83 | Cannot verify PIN |
| 85 | No reason to decline (used for verification-only requests) |
| 91 | Issuer or switch inoperative |
| 92 | Financial institution not found for routing |
| 93 | Transaction cannot be completed, violation of law |
| 94 | Duplicate transmission |
| 96 | System malfunction |

---

## DE 40 — Service restriction code

3 digits, defines geographic/usage restrictions on the card (issuer-specific in practice — base standard leaves this largely open). Rarely standardized across networks; check your issuer's card program docs.

---

## DE 60 — Advice/reason code (national use)

Free-form per network — no ISO base standard values. Typically used for STIP (Stand-In Processing) advice reason codes, reversal reasons, etc. Entirely dependent on which national/domestic switch you're integrating with.

---

## DE 70 — Network management information code

3 digits, used in network management messages (MTI `08xx`) rather than financial transactions — this is how a terminal or switch keeps its connection alive and synchronized with the host.

| Code | Meaning |
|------|---------|
| 001 | Sign-on |
| 002 | Sign-off |
| 161 | Echo test / key exchange |
| 201 | Cutover (start of day/batch) |
| 301 | Echo test |
| 302 | Key change |

---

## DE 93 — Response indicator

5 digits, used in some networks to give more granular reasoning than DE 39 alone. Not broadly standardized — issuer/switch-specific.

---

## DEs with no meaningful "code mapping"

Worth being explicit about what's *not* in this document, and why:

- **DE 2, 34** (PAN) — the value itself is the card number, not a coded reference to anything.
- **DE 4–10, 82–89, 97** (amounts) — raw numeric values, not codes.
- **DE 7, 12–17, 71, 73** (dates/times) — literal timestamps, not codes.
- **DE 11, 37** (STAN, RRN) — sequential/unique identifiers, not codes.
- **DE 35, 36, 45** (track data) — raw card track contents.
- **DE 41–43, 98, 101–103** (terminal/merchant/account identifiers, names) — free text/identifiers assigned by the acquirer or issuer, not from a published list.
- **DE 52, 55, 64, 96, 128** (PIN block, EMV data, MAC fields) — binary cryptographic material, not enumerated values.
- **DE 46–48, 54, 56–63, 105–127** (private/reserved/additional data) — open containers for whatever the processor wants to put there; meaning is defined per-vendor, not per-standard.

If you're building the parser out further, DE 3, 22, 25, and 39 are the ones worth actually encoding as lookup tables/enums in code, since your app logic will genuinely branch on them (e.g. `if response_code == "00": approved`). The rest are either free text or too vendor-specific to hardcode meaningfully.
