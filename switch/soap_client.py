"""
SOAP 1.1 client for the IBM ACE ISO 8583 gateway.

Self-contained on purpose: envelope construction, fault handling, and the
three operations, with no dependency beyond httpx. This mirrors
libs/mfcommon/mfcommon/soap/ in the microservices repo -- two repositories,
one WSDL contract, which is what keeps them interoperable.

WHY HAND-ROLLED, NOT zeep. zeep is the obvious choice and was deliberately
rejected. It fetches and compiles the WSDL at client construction, so
process startup acquires a hard network dependency on ACE being reachable,
and the request shape becomes whatever zeep infers rather than something
explicit and version-controlled. The contract here is three operations and a
dozen fields; it does not need a WSDL compiler.

It is also the house style. The JWT implementation, the BCD codec, and the
ISO 9564 PIN block in this repository are all hand-rolled for the same
reason: at a protocol boundary, knowing exactly what goes out matters more
than the convenience of a library.

THE FAULT TRAP. A SOAP fault is not an HTTP error. It arrives as HTTP 500
with a well-formed <soap:Fault> in the body, and some stacks return faults
as HTTP 200. Code that branches on status_code alone either retries a
transaction the switch explicitly refused, or parses a fault as success.
parse_response() checks for a Fault element FIRST, before anything else and
regardless of status.
"""

import xml.etree.ElementTree as ET

SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-wssecurity-secext-1.0.xsd")
PASSWORD_TEXT_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                      "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Must match the WSDL exactly. ACE validates the request namespace against
# the deployed contract and rejects a mismatch with an unhelpful parse error
# rather than a clear one.
ISO8583_NS = "urn:microfinance:iso8583:v1"

ET.register_namespace("soapenv", SOAP_ENV_NS)
ET.register_namespace("iso", ISO8583_NS)
ET.register_namespace("wsse", WSSE_NS)


class SoapFault(Exception):
    """A fault the peer deliberately returned. The code distinguishes the two
    cases needing opposite handling: Client.* means our request was bad and
    retrying sends the identical bad request; Server.* may be transient."""

    def __init__(self, code, string, detail=None):
        self.code = code
        self.string = string
        self.detail = detail
        super().__init__(f"SOAP Fault [{code}]: {string}")

    @property
    def is_client_fault(self) -> bool:
        return self.code.split(":")[-1].lower().startswith("client")


class SoapTimeout(Exception):
    """No response before the deadline. The outcome is genuinely UNKNOWN --
    the switch may have authorized and only the answer was lost. Callers must
    reverse rather than assume either success or failure."""


class SoapTransportError(Exception):
    """Could not reach ACE at all. Nothing was sent, so no reversal needed."""


class SoapProtocolError(Exception):
    """The response was not a parseable SOAP envelope."""


def _qn(namespace, tag):
    return f"{{{namespace}}}{tag}"


def _preview(raw, limit=200):
    text = raw[:limit].decode("utf-8", errors="replace").strip()
    return f"{text}{'...' if len(raw) > limit else ''}"


def build_envelope(body_tag, fields, username=None, password=None):
    """
    Values of None are OMITTED, not sent as empty elements.

    This is not cosmetic: in ISO 8583 an absent data element (bitmap bit
    clear) and a present-but-empty one are different messages, and a real
    switch answers the second with DE 39 = 30 (format error).
    """
    envelope = ET.Element(_qn(SOAP_ENV_NS, "Envelope"))
    header = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Header"))

    if username is not None:
        security = ET.SubElement(header, _qn(WSSE_NS, "Security"))
        security.set(_qn(SOAP_ENV_NS, "mustUnderstand"), "1")
        token = ET.SubElement(security, _qn(WSSE_NS, "UsernameToken"))
        ET.SubElement(token, _qn(WSSE_NS, "Username")).text = username
        pw = ET.SubElement(token, _qn(WSSE_NS, "Password"))
        pw.set("Type", PASSWORD_TEXT_TYPE)
        pw.text = password or ""

    body = ET.SubElement(envelope, _qn(SOAP_ENV_NS, "Body"))
    operation = ET.SubElement(body, _qn(ISO8583_NS, body_tag))
    for name, value in fields.items():
        if value is None:
            continue
        ET.SubElement(operation, _qn(ISO8583_NS, name)).text = str(value)

    return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(envelope, encoding="utf-8")


def parse_response(raw):
    """Returns the body's children as a flat dict, or raises SoapFault."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SoapProtocolError(f"Not well-formed XML: {exc}. Received: {_preview(raw)!r}")

    # Checked explicitly, because well-formed NON-SOAP XML is a routine
    # failure: an ingress returning an HTML error page parses perfectly
    # happily. "No Body element" would be true and useless -- the reader
    # needs to know an HTML page arrived.
    if root.tag != _qn(SOAP_ENV_NS, "Envelope"):
        raise SoapProtocolError(
            f"Root element is <{root.tag}>, not a SOAP Envelope -- a proxy or ingress "
            f"probably answered instead of the service. Received: {_preview(raw)!r}"
        )

    body = root.find(_qn(SOAP_ENV_NS, "Body"))
    if body is None:
        raise SoapProtocolError(f"No SOAP Body. Received: {_preview(raw)!r}")

    fault = body.find(_qn(SOAP_ENV_NS, "Fault"))
    if fault is not None:
        # SOAP 1.1 fault children are UNQUALIFIED -- no namespace, unlike
        # everything else in the envelope. Parsers that look for them in the
        # envelope namespace find nothing and report a fault with no message.
        code = (fault.findtext("faultcode") or "unknown").strip()
        string = (fault.findtext("faultstring") or "no faultstring provided").strip()
        detail_el = fault.find("detail")
        detail = ET.tostring(detail_el, encoding="unicode").strip() if detail_el is not None else None
        raise SoapFault(code, string, detail)

    if len(body) == 0:
        raise SoapProtocolError("SOAP Body is empty -- expected a response element")

    return {child.tag.split("}", 1)[-1]: (child.text or "") for child in body[0]}


class Iso8583SoapClient:
    def __init__(self, endpoint, timeout=20.0, username=None, password=None):
        import httpx

        self.endpoint = endpoint
        self.timeout = timeout
        self.username = username
        self.password = password
        self._client = httpx.Client(timeout=timeout)

    def close(self):
        self._client.close()

    def _call(self, operation, fields, timeout=None):
        import httpx

        payload = build_envelope(operation, fields, self.username, self.password)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            # Mandatory in SOAP 1.1, and ACE routes on it. Omitting it
            # produces an HTTP 500 whose message never mentions SOAPAction.
            "SOAPAction": f'"{ISO8583_NS}/{operation}"',
        }

        try:
            response = self._client.post(
                self.endpoint, content=payload, headers=headers, timeout=timeout or self.timeout
            )
        except httpx.TimeoutException as exc:
            raise SoapTimeout(
                f"No response from {self.endpoint} for {operation} within "
                f"{timeout or self.timeout}s -- outcome UNKNOWN"
            ) from exc
        except httpx.RequestError as exc:
            raise SoapTransportError(f"Cannot reach {self.endpoint}: {exc!r}") from exc

        # No status check before parsing: a fault legitimately arrives as 500.
        try:
            return parse_response(response.content)
        except SoapProtocolError:
            if response.status_code >= 500:
                raise SoapTransportError(
                    f"{self.endpoint} returned HTTP {response.status_code} with a non-SOAP body"
                )
            raise

    def authorize(self, *, pan, processing_code, amount_minor, entry_mode, rrn,
                  currency_code, pin_block_hex=None, ksn=None, stan=None,
                  account_id_2=None, additional_data=None, timeout=None):
        """MTI 0200 -> 0210."""
        return self._call("authorizeRequest", {
            "pan": pan,
            "processingCode": processing_code,
            "amountMinor": amount_minor,
            "entryMode": entry_mode,
            "rrn": rrn,
            "currencyCode": currency_code,
            "pinBlockHex": pin_block_hex,
            "ksn": ksn,
            "stan": stan,
            "accountId2": account_id_2,
            "additionalData": additional_data,
        }, timeout=timeout)

    def reverse(self, *, original_mti, original_stan, rrn, amount_minor, pan, timeout=None):
        """MTI 0400 -> 0410. Idempotent at the switch, so safe -- and
        necessary -- to retry until acknowledged."""
        return self._call("reverseRequest", {
            "originalMti": original_mti,
            "originalStan": original_stan,
            "rrn": rrn,
            "amountMinor": amount_minor,
            "pan": pan,
        }, timeout=timeout)

    def network_management(self, *, code="301", timeout=None):
        """MTI 0800 -> 0810. 001 sign-on, 002 sign-off, 301 echo."""
        return self._call("networkManagementRequest", {"networkCode": code}, timeout=timeout)
