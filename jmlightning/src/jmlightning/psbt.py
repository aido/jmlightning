from __future__ import annotations

import base64
import binascii


def psbt_from_base64(value: str) -> bytes:
    """Decode a PSBT from strict base64 text."""
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid PSBT base64 encoding") from exc
