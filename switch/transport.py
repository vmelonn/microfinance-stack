"""
Transport abstraction: how a transaction reaches the switch.

Stages 1-3 built one way of doing this -- build the message here, frame it
with an MLI, push it down a socket we own, and correlate the response by
STAN. That still works and is still the default. What this file adds is a
SECOND way: hand a SOAP request to IBM ACE and let it do the message
building, the framing, the socket, and the correlation.

    api/routes/transactions.py
              │
              ▼
    ┌─────────────────────────────┐
    │   Iso8583Transport (ABC)    │
    └────┬───────────────────┬────┘
         │                   │
  DirectTcpTransport   AceSoapTransport
  iso8583/ + switch/   SOAP/WSDL -> ACE -> DFDL -> MLI -> host
  + correlation/       (ACE owns all of it)

Selected by ISO8583_TRANSPORT=direct|ace.

WHY KEEP BOTH. The Python codec is not dead code once ACE arrives -- it is
the executable specification the DFDL schema has to agree with, it is what
the test suite runs against, and it is what still works when the ACE
integration server is down for maintenance. Deleting it in favour of ACE
would trade a tested implementation for an untested one and lose the
reference.

The interface is deliberately narrow: three operations, no ISO 8583 concepts
in the signatures. A caller does not pass DE numbers, an MTI, or a STAN,
because under the ACE transport this process never sees any of them -- ACE
assigns the STAN and builds the message. Anything wider than this would leak
the direct transport's internals into the contract and stop the two being
interchangeable.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AuthorizationResult:
    """
    What comes back from an authorization attempt.

    THREE outcomes, not two. "unknown" exists because a network call can
    succeed while its response is lost: the switch may have approved and
    debited the cardholder, and only the answer went missing. Collapsing
    that into "declined" means recording nothing for money that genuinely
    moved. Callers must handle it as its own case -- see
    api/routes/transactions.py, which skips the ledger posting for it.
    """
    outcome: str                  # "approved" | "declined" | "unknown"
    response_code: str = ""
    response_text: str = ""
    authorization_id: str = None
    stan: str = None
    rrn: str = None
    reversal_sent: bool = False


class Iso8583Transport(ABC):
    @abstractmethod
    def authorize(self, *, pan, processing_code, amount_minor, entry_mode,
                  rrn, currency_code, pin_block, ksn, account_id_2=None) -> AuthorizationResult:
        ...

    @abstractmethod
    def reverse(self, *, original_stan, rrn, amount_minor, pan) -> bool:
        """Returns whether the reversal was acknowledged."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Backs GET /health. Must reflect the real path to the switch, not
        merely that this process is running."""
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class DirectTcpTransport(Iso8583Transport):
    """
    The original path, unchanged in behaviour: Stage 1 builds the message,
    Stage 2 owns the socket, Stage 3 correlates by STAN and fires a reversal
    on timeout.
    """

    def __init__(self, client, correlator):
        self.client = client
        self.correlator = correlator

    def authorize(self, *, pan, processing_code, amount_minor, entry_mode,
                  rrn, currency_code, pin_block, ksn, account_id_2=None) -> AuthorizationResult:
        from correlation.tracker import TransactionTimeout
        from iso8583.parser import DE39_RESPONSE_CODES

        fields = {
            2: pan,
            3: processing_code,
            4: amount_minor,
            22: entry_mode,
            37: rrn,
            49: currency_code,
        }
        if pin_block is not None:
            # latin-1 is a byte-for-byte carrier for 0x00-0xFF, so this is a
            # lossless bytes->str move, not a text decode.
            fields[52] = pin_block.decode("latin-1")
        if ksn is not None:
            fields[53] = ksn.rjust(16, "0")
        if account_id_2 is not None:
            fields[103] = account_id_2

        try:
            response = self.correlator.send_and_wait("0200", fields)
        except TransactionTimeout as exc:
            # The correlation manager has already sent the reversal. The
            # business outcome is still unknown.
            return AuthorizationResult(
                outcome="unknown", response_text=str(exc), rrn=rrn, reversal_sent=True
            )
        except (ConnectionError, OSError) as exc:
            # Never reached the switch, so nothing was authorized and no
            # reversal is needed. Distinct from a timeout on purpose.
            return AuthorizationResult(
                outcome="unknown", response_text=f"Switch unreachable: {exc}", rrn=rrn
            )

        code = response["fields"].get(39, "")
        return AuthorizationResult(
            outcome="approved" if code == "00" else "declined",
            response_code=code,
            response_text=DE39_RESPONSE_CODES.get(code, f"Unmapped response code {code!r}"),
            authorization_id=response["fields"].get(38),
            stan=response["fields"].get(11),
            rrn=response["fields"].get(37, rrn),
        )

    def reverse(self, *, original_stan, rrn, amount_minor, pan) -> bool:
        try:
            self.correlator._send_reversal("0200", original_stan)
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        return self.client._connected.is_set()

    def close(self) -> None:
        self.client.close()


class AceSoapTransport(Iso8583Transport):
    """
    IBM ACE does the mediation: SOAP in, DFDL-serialized ISO 8583 out over
    its own MLI-framed TCP connection, correlation and STAN allocation
    included.

    This process stops owning a socket entirely. That is the point -- an ESB
    exists to be the thing that speaks the awkward protocol.

    Points at either a real ACE integration server or at the microservices
    repo's ace-stub, which serves the identical WSDL. Same contract either
    way.
    """

    def __init__(self, endpoint, timeout=20.0, username=None, password=None):
        from switch.soap_client import Iso8583SoapClient

        self.soap = Iso8583SoapClient(endpoint, timeout=timeout, username=username, password=password)
        self.endpoint = endpoint

    def authorize(self, *, pan, processing_code, amount_minor, entry_mode,
                  rrn, currency_code, pin_block, ksn, account_id_2=None) -> AuthorizationResult:
        from switch.soap_client import SoapFault, SoapTimeout, SoapTransportError

        try:
            result = self.soap.authorize(
                pan=pan,
                processing_code=processing_code,
                amount_minor=amount_minor,
                entry_mode=entry_mode,
                rrn=rrn,
                currency_code=currency_code,
                # HEX, not raw bytes. An 8-byte PIN block contains arbitrary
                # byte values, several of which are ILLEGAL in XML 1.0 with
                # no escape sequence that makes them legal -- so the raw form
                # simply cannot cross this boundary.
                pin_block_hex=pin_block.hex() if pin_block else None,
                ksn=ksn,
                account_id_2=account_id_2,
            )
        except SoapFault as fault:
            detail = fault.detail or ""
            if "SWITCH_TIMEOUT" in detail:
                return AuthorizationResult(
                    outcome="unknown", response_text=fault.string, rrn=rrn, reversal_sent=True
                )
            if "SWITCH_DOWN" in detail:
                return AuthorizationResult(
                    outcome="unknown", response_text=f"Switch unavailable: {fault.string}", rrn=rrn
                )
            # A Client fault means we built a bad request; retrying sends the
            # identical bad request. Reported as declined rather than
            # unknown, because nothing reached the switch.
            return AuthorizationResult(
                outcome="declined", response_code="30", response_text=fault.string, rrn=rrn
            )
        except SoapTimeout as exc:
            # We gave up before ACE answered, so we do not know whether ACE
            # sent a reversal. Send one ourselves: a duplicate reversal is
            # acknowledged harmlessly, a missing one is not.
            sent = self.reverse(original_stan="000000", rrn=rrn, amount_minor=amount_minor, pan=pan)
            return AuthorizationResult(
                outcome="unknown", response_text=str(exc), rrn=rrn, reversal_sent=sent
            )
        except SoapTransportError as exc:
            return AuthorizationResult(
                outcome="unknown", response_text=f"ACE unreachable: {exc}", rrn=rrn
            )

        code = result.get("responseCode", "")
        return AuthorizationResult(
            outcome="approved" if code == "00" else "declined",
            response_code=code,
            response_text=result.get("responseText", ""),
            authorization_id=result.get("authId"),
            stan=result.get("stan"),
            rrn=result.get("rrn") or rrn,
        )

    def reverse(self, *, original_stan, rrn, amount_minor, pan) -> bool:
        try:
            self.soap.reverse(
                original_mti="0200", original_stan=original_stan,
                rrn=rrn, amount_minor=amount_minor, pan=pan, timeout=10.0,
            )
            return True
        except Exception as exc:
            print(f"[AceSoapTransport] REVERSAL FAILED for rrn={rrn}: {exc!r}. "
                  f"Cardholder may remain debited for an unrecorded transaction.")
            return False

    def is_connected(self) -> bool:
        """An echo test through the whole chain: here -> ACE -> TCP -> switch.
        A health check that only proved this process was up would report
        healthy while every transaction failed."""
        try:
            self.soap.network_management(code="301", timeout=5.0)
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.soap.close()


def build_transport(client=None, correlator=None) -> Iso8583Transport:
    """
    Reads ISO8583_TRANSPORT and returns the selected transport.

    Same opt-in-by-environment-variable pattern as REDIS_URL and
    HSM_KEY_PERSISTENCE_PATH elsewhere in this project: the default is the
    behaviour that needs no external setup, and the alternative is one
    variable away.
    """
    mode = os.environ.get("ISO8583_TRANSPORT", "direct").lower()

    if mode == "ace":
        endpoint = os.environ.get("ISO8583_SOAP_ENDPOINT", "http://localhost:8090/Iso8583Gateway")
        print(f"[transport] ISO8583_TRANSPORT=ace -- routing through {endpoint}. "
              f"The local iso8583/switch/correlation layers are bypassed; ACE owns "
              f"message building, framing, and STAN correlation.")
        return AceSoapTransport(
            endpoint,
            timeout=float(os.environ.get("ISO8583_SOAP_TIMEOUT_SECONDS", "20")),
            username=os.environ.get("ISO8583_SOAP_USERNAME"),
            password=os.environ.get("ISO8583_SOAP_PASSWORD"),
        )

    if mode != "direct":
        raise ValueError(
            f"ISO8583_TRANSPORT={mode!r} is not recognised. Use 'direct' or 'ace'."
        )

    print("[transport] ISO8583_TRANSPORT=direct -- using the built-in ISO 8583 "
          "codec and TCP client.")
    return DirectTcpTransport(client, correlator)
