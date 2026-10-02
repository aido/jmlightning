from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

from jmcore.bitcoin import ParsedTransaction, encode_varint
from jmwallet.wallet.psbt import (
    PSBT_IN_FINAL_SCRIPTWITNESS,
    PSBT_IN_PARTIAL_SIG,
    ParsedPSBT,
)

from jmlightning.models import ClassifiedUTXO


def psbt_from_base64(value: str) -> bytes:
    """Decode a PSBT from strict base64 text."""
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid PSBT base64 encoding") from exc


def finalise_psbt_inputs(
    parsed_psbt: ParsedPSBT,
    signing_inputs: Mapping[int, ClassifiedUTXO],
    tx: ParsedTransaction,
) -> None:
    """Replace JM partial signatures with final P2WPKH witnesses."""
    for index in signing_inputs:
        input_map = parsed_psbt.input_maps[index]
        signatures = [
            record
            for record in input_map.records
            if record.key[:1] == bytes([PSBT_IN_PARTIAL_SIG])
        ]
        if len(signatures) != 1:
            raise RuntimeError(
                "JoinMarket wallet did not return exactly one signature "
                f"for input {index}"
            )

        signature = signatures[0].value
        pubkey = signatures[0].key[1:]
        witness = encode_varint(2) + encode_varint(len(signature)) + signature
        witness += encode_varint(len(pubkey)) + pubkey

        input_map.records = [
            record
            for record in input_map.records
            if record.key[:1] != bytes([PSBT_IN_PARTIAL_SIG])
        ]
        input_map.append(bytes([PSBT_IN_FINAL_SCRIPTWITNESS]), witness)

        if len(tx.witnesses) < len(tx.inputs):
            tx.witnesses.extend([[] for _ in range(len(tx.inputs) - len(tx.witnesses))])
        tx.witnesses[index] = [signature, pubkey]
