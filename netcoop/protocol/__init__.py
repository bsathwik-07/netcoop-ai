"""The NetCoop message protocol: vocabulary, payloads, envelope, migrations."""

from netcoop.protocol.envelope import (
    BROADCAST,
    MESSAGE_MODELS,
    SYSTEM_CORRELATION,
    Envelope,
    ProtocolError,
    decode_message,
    make_message,
)
from netcoop.protocol.vocabulary import PROTOCOL, ErrorCode, Hypothesis, MessageType

__all__ = [
    "BROADCAST",
    "MESSAGE_MODELS",
    "PROTOCOL",
    "SYSTEM_CORRELATION",
    "Envelope",
    "ErrorCode",
    "Hypothesis",
    "MessageType",
    "ProtocolError",
    "decode_message",
    "make_message",
]
