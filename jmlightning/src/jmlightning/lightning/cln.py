import secrets
from dataclasses import dataclass
from math import isfinite

from jmcore.bitcoin import (
    decode_varint,
    encode_varint,
    parse_transaction_bytes,
    psbt_to_base64,
)
from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_UNSIGNED_TX,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_NON_WITNESS_UTXO,
    PSBT_IN_PROPRIETARY,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    ParsedPSBT,
    PSBTError,
    parse_psbt,
)
from pyln.client import LightningRpc

from jmlightning.lightning.backend import (
    ChannelFundingStatus,
    FeePriority,
    LightningBackend,
)


@dataclass(frozen=True)
class FeeEstimate:
    blocks: int
    sat_per_kvb: int


# CLN splice PSBT compatibility. These fields and weight rules mirror the
# public CLN PSBT/transaction protocol, but pyln-client does not expose
# helpers for them. Keep the compatibility fork here so TxBuilder remains
# focused on transaction construction and validation.
CLN_PSBT_SERIAL_ID_KEY = bytes([PSBT_IN_PROPRIETARY]) + b"\x09lightning\x01"

PSBT_GLOBAL_TX_VERSION = 0x02
PSBT_GLOBAL_FALLBACK_LOCKTIME = 0x03
PSBT_GLOBAL_INPUT_COUNT = 0x04
PSBT_GLOBAL_OUTPUT_COUNT = 0x05
PSBT_GLOBAL_TX_MODIFIABLE = 0x06
PSBT_IN_PREVIOUS_TXID = 0x0E
PSBT_IN_OUTPUT_INDEX = 0x0F
PSBT_IN_SEQUENCE = 0x10
PSBT_OUT_AMOUNT = 0x03
PSBT_OUT_SCRIPT = 0x04


def normalise_psbt_v2_to_v0(psbt: bytes) -> bytes:
    """Convert CLN's BIP370 PSBT v2 to the BIP174 form used by jmwallet."""
    magic = PSBT_MAGIC
    if not psbt.startswith(magic):
        raise ValueError("invalid PSBT magic")

    def _read_compact_size(data: bytes, offset: int) -> tuple[int, int]:
        if offset >= len(data):
            raise ValueError("truncated PSBT compact size")
        first = data[offset]
        offset += 1
        if first < 0xFD:
            return first, offset
        size = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
        end = offset + size
        if end > len(data):
            raise ValueError("truncated PSBT compact size")
        value = int.from_bytes(data[offset:end], "little")
        minimum = {0xFD: 0xFD, 0xFE: 0x10000, 0xFF: 0x100000000}[first]
        if value < minimum:
            raise ValueError("noncanonical PSBT compact size")
        return value, end

    def _read_map(data: bytes, offset: int) -> tuple[list[tuple[bytes, bytes]], int]:
        records: list[tuple[bytes, bytes]] = []
        while True:
            key_len, offset = _read_compact_size(data, offset)
            if key_len == 0:
                return records, offset
            key_end = offset + key_len
            if key_end > len(data):
                raise ValueError("truncated PSBT key")
            key = data[offset:key_end]
            offset = key_end
            value_len, offset = _read_compact_size(data, offset)
            value_end = offset + value_len
            if value_end > len(data):
                raise ValueError("truncated PSBT value")
            records.append((key, data[offset:value_end]))
            offset = value_end

    def _write_map(records: list[tuple[bytes, bytes]]) -> bytes:
        result = bytearray()
        for key, value in records:
            result.extend(encode_varint(len(key)))
            result.extend(key)
            result.extend(encode_varint(len(value)))
            result.extend(value)
        result.append(0)
        return bytes(result)

    offset = len(magic)
    global_records, offset = _read_map(psbt, offset)
    version_records = [
        value for key, value in global_records if key == bytes([PSBT_GLOBAL_VERSION])
    ]
    if not version_records:
        return psbt
    if len(version_records) != 1 or len(version_records[0]) != 4:
        raise ValueError("invalid PSBT version record")
    version = int.from_bytes(version_records[0], "little")
    if version == 0:
        return psbt
    if version != 2:
        raise ValueError(f"unsupported PSBT version: {version}")

    input_count_records = [
        value
        for key, value in global_records
        if key == bytes([PSBT_GLOBAL_INPUT_COUNT])
    ]
    output_count_records = [
        value
        for key, value in global_records
        if key == bytes([PSBT_GLOBAL_OUTPUT_COUNT])
    ]
    tx_version_records = [
        value for key, value in global_records if key == bytes([PSBT_GLOBAL_TX_VERSION])
    ]
    locktime_records = [
        value
        for key, value in global_records
        if key == bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME])
    ]
    if len(input_count_records) != 1 or len(output_count_records) != 1:
        raise ValueError("PSBT v2 is missing input/output counts")
    if len(tx_version_records) != 1 or len(tx_version_records[0]) != 4:
        raise ValueError("PSBT v2 has an invalid transaction version")
    if len(locktime_records) > 1 or (
        locktime_records and len(locktime_records[0]) != 4
    ):
        raise ValueError("PSBT v2 has an invalid fallback locktime")

    def _decode_count(value: bytes, context: str) -> int:
        if not value:
            raise ValueError(f"PSBT v2 {context} count is empty")
        count, end = _read_compact_size(value, 0)
        if end != len(value):
            raise ValueError(f"PSBT v2 {context} count has trailing data")
        return count

    input_count = _decode_count(input_count_records[0], "input")
    output_count = _decode_count(output_count_records[0], "output")
    input_maps: list[list[tuple[bytes, bytes]]] = []
    for _ in range(input_count):
        records, offset = _read_map(psbt, offset)
        input_maps.append(records)
    output_maps: list[list[tuple[bytes, bytes]]] = []
    for _ in range(output_count):
        records, offset = _read_map(psbt, offset)
        output_maps.append(records)
    if offset != len(psbt):
        raise ValueError("trailing data after PSBT maps")

    def _singleton(
        records: list[tuple[bytes, bytes]], key_type: bytes, context: str
    ) -> bytes:
        values = [value for key, value in records if key == key_type]
        if len(values) != 1:
            raise ValueError(f"PSBT v2 {context} record must occur exactly once")
        return values[0]

    tx = bytearray()
    tx.extend(tx_version_records[0])
    tx.extend(encode_varint(input_count))
    for index, records in enumerate(input_maps):
        txid = _singleton(
            records, bytes([PSBT_IN_PREVIOUS_TXID]), f"input {index} previous txid"
        )
        vout = _singleton(
            records, bytes([PSBT_IN_OUTPUT_INDEX]), f"input {index} output index"
        )
        sequence_values = [
            value for key, value in records if key == bytes([PSBT_IN_SEQUENCE])
        ]
        if len(txid) != 32 or len(vout) != 4:
            raise ValueError(f"PSBT v2 input {index} has invalid outpoint")
        if len(sequence_values) > 1 or (
            sequence_values and len(sequence_values[0]) != 4
        ):
            raise ValueError(f"PSBT v2 input {index} has invalid sequence")
        tx.extend(txid)
        tx.extend(vout)
        tx.append(0)
        tx.extend(sequence_values[0] if sequence_values else b"\xff\xff\xff\xff")
    tx.extend(encode_varint(output_count))
    for index, records in enumerate(output_maps):
        amount = _singleton(records, bytes([PSBT_OUT_AMOUNT]), f"output {index} amount")
        script = _singleton(records, bytes([PSBT_OUT_SCRIPT]), f"output {index} script")
        if len(amount) != 8:
            raise ValueError(f"PSBT v2 output {index} has invalid amount")
        tx.extend(amount)
        tx.extend(encode_varint(len(script)))
        tx.extend(script)
    tx.extend(locktime_records[0] if locktime_records else b"\x00\x00\x00\x00")

    v2_globals_to_remove = {
        bytes([PSBT_GLOBAL_UNSIGNED_TX]),
        bytes([PSBT_GLOBAL_TX_VERSION]),
        bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]),
        bytes([PSBT_GLOBAL_INPUT_COUNT]),
        bytes([PSBT_GLOBAL_OUTPUT_COUNT]),
        bytes([PSBT_GLOBAL_TX_MODIFIABLE]),
        bytes([PSBT_GLOBAL_VERSION]),
    }
    v0_globals = [
        (key, value) for key, value in global_records if key not in v2_globals_to_remove
    ]
    v0_globals.insert(0, (bytes([PSBT_GLOBAL_UNSIGNED_TX]), bytes(tx)))
    result = bytearray(magic)
    result.extend(_write_map(v0_globals))
    for records in input_maps:
        result.extend(
            _write_map(
                [
                    (key, value)
                    for key, value in records
                    if key
                    not in {
                        bytes([PSBT_IN_PREVIOUS_TXID]),
                        bytes([PSBT_IN_OUTPUT_INDEX]),
                        bytes([PSBT_IN_SEQUENCE]),
                    }
                ]
            )
        )
    for records in output_maps:
        result.extend(
            _write_map(
                [
                    (key, value)
                    for key, value in records
                    if key
                    not in {
                        bytes([PSBT_OUT_AMOUNT]),
                        bytes([PSBT_OUT_SCRIPT]),
                    }
                ]
            )
        )
    return bytes(result)


def new_serial_id(existing: set[int]) -> int:
    """Generate a fresh even-parity CLN initiator serial ID."""
    while True:
        serial_id = secrets.randbits(63) << 1
        if serial_id != 0 and serial_id not in existing:
            return serial_id


def serial_ids(parsed_psbt: ParsedPSBT) -> set[int]:
    """Return all existing CLN interactive transaction serial IDs."""
    serial_ids: set[int] = set()
    for psbt_map in [*parsed_psbt.input_maps, *parsed_psbt.output_maps]:
        for record in psbt_map.records:
            if record.key != CLN_PSBT_SERIAL_ID_KEY:
                continue
            if len(record.value) != 8:
                raise ValueError("Invalid Core Lightning serial ID")
            serial_ids.add(int.from_bytes(record.value, "big"))
    return serial_ids


def _input_weight(parsed_psbt: ParsedPSBT, index: int) -> int:
    input_map = parsed_psbt.input_maps[index]
    witness_records = [
        record
        for record in input_map.records
        if record.key[:1] == bytes([PSBT_IN_WITNESS_UTXO])
    ]
    script: bytes | None = None
    if len(witness_records) == 1:
        value = witness_records[0].value
        if len(value) < 9:
            raise ValueError("Invalid witness UTXO record")
        script_len, offset = decode_varint(value, 8)
        if offset + script_len != len(value):
            raise ValueError("Invalid witness UTXO record")
        script = value[offset:]
    if script is None:
        non_witness_records = [
            record
            for record in input_map.records
            if record.key[:1] == bytes([PSBT_IN_NON_WITNESS_UTXO])
        ]
        if len(non_witness_records) == 1:
            previous_transaction = parse_transaction_bytes(non_witness_records[0].value)
            vout = parsed_psbt.transaction.inputs[index].vout
            if vout >= len(previous_transaction.outputs):
                raise ValueError("Invalid non-witness UTXO record")
            script = previous_transaction.outputs[vout].script
    if script is None:
        raise ValueError("Splice input is missing UTXO data")
    if script.startswith(b"\x00\x14"):
        return 271
    if script.startswith(b"\x00\x20"):
        return 387
    if script.startswith(b"\x51\x20"):
        return 230
    raise ValueError("Unsupported splice input script type")


def _output_weight(script: bytes) -> int:
    return (8 + len(encode_varint(len(script))) + len(script)) * 4


def _core_weight(num_inputs: int, num_outputs: int) -> int:
    return (
        4 + len(encode_varint(num_inputs)) + len(encode_varint(num_outputs)) + 4
    ) * 4 + 2


def estimate_splice_fee(
    psbt: bytes,
    feerate_per_kw: int,
    add_change_output: bool = True,
) -> tuple[int, int]:
    """Estimate a CLN splice initiator fee using CLN's weight rules."""
    if feerate_per_kw <= 0:
        raise ValueError("Fee rate must be positive")
    try:
        parsed_psbt = parse_psbt(normalise_psbt_v2_to_v0(psbt))
    except PSBTError as exc:
        raise ValueError("Invalid splice PSBT") from exc
    input_weight = sum(
        _input_weight(parsed_psbt, index)
        for index in range(len(parsed_psbt.input_maps))
    )
    output_weight = sum(
        _output_weight(output.script) for output in parsed_psbt.transaction.outputs
    )
    input_weight += 271
    output_count = len(parsed_psbt.transaction.outputs)
    if add_change_output:
        output_weight += 124
        output_count += 1
    weight = (
        input_weight
        + output_weight
        + _core_weight(len(parsed_psbt.transaction.inputs) + 1, output_count)
    )
    return (feerate_per_kw * weight) // 1000, weight


class CLNBackend(LightningBackend):
    def __init__(self, socket_path: str):
        # Hooks up to the Unix socket we passed in config
        self.rpc = LightningRpc(socket_path)

    def _estimate_fees(self) -> list[FeeEstimate]:
        """
        Fetch and validate fee estimates from CLN.

        Returns a list of FeeEstimate objects sorted by confirmation target.
        """
        try:
            result = self.rpc.estimatefees()
        except Exception as exc:
            raise RuntimeError(f"Failed to retrieve fee estimates: {exc}") from exc

        if not isinstance(result, dict):
            raise RuntimeError("CLN estimatefees returned an invalid response")

        raw_feerates = result.get("feerates")

        if not isinstance(raw_feerates, list) or not raw_feerates:
            raise RuntimeError(
                "CLN estimatefees returned invalid or empty feerates data"
            )

        estimates: list[FeeEstimate] = []

        for entry in raw_feerates:
            if not isinstance(entry, dict):
                raise RuntimeError(
                    "CLN estimatefees returned an invalid fee estimate entry"
                )

            blocks = entry.get("blocks")
            sat_per_kvb = entry.get("feerate")

            if not isinstance(blocks, int) or isinstance(blocks, bool) or blocks <= 0:
                raise RuntimeError(
                    "CLN estimatefees returned an invalid confirmation target"
                )

            if (
                not isinstance(sat_per_kvb, int)
                or isinstance(sat_per_kvb, bool)
                or sat_per_kvb <= 0
            ):
                raise RuntimeError("CLN estimatefees returned an invalid fee rate")

            estimates.append(
                FeeEstimate(
                    blocks=blocks,
                    sat_per_kvb=sat_per_kvb,
                )
            )

        return estimates

    def open_channel_start(
        self,
        peer_id: str,
        amount: int,
        announce: bool = False,
    ) -> str:
        """Ask CLN for the 2-of-2 multisig address to fund the channel."""
        try:
            result = self.rpc.fundchannel_start(
                peer_id,
                amount,
                announce=announce,
            )
            funding_address = result.get("funding_address")

            if not isinstance(funding_address, str):
                raise RuntimeError(
                    "CLN fundchannel_start response is missing funding_address"
                )

            return funding_address
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to start channel open: {exc}") from exc

    def open_channel_complete(
        self,
        peer_id: str,
        psbt: bytes,
    ) -> dict[str, object]:
        """Complete channel establishment using the funding transaction PSBT."""
        try:
            result = self.rpc.fundchannel_complete(
                node_id=peer_id,
                psbt=psbt_to_base64(psbt),
                withhold=True,
            )
            return dict(result)
        except Exception as exc:
            raise RuntimeError(f"Failed to complete channel open: {exc}") from exc

    def cancel_channel_funding(
        self,
        peer_id: str,
    ) -> None:
        """Cancel a funding operation before its funding transaction is broadcast."""
        try:
            self.rpc.fundchannel_cancel(node_id=peer_id)
        except Exception as exc:
            raise RuntimeError(f"Failed to cancel channel funding: {exc}") from exc

    def splice_init(
        self,
        channel_id: str,
        amount: int,
        initial_psbt: bytes | None = None,
        feerate_per_kw: int | None = None,
        force_feerate: bool = False,
    ) -> dict[str, object]:
        """Initiate a CLN channel splice."""
        try:
            if force_feerate:
                raise RuntimeError(
                    "pyln-client splice_init wrapper does not support force_feerate"
                )

            result = self.rpc.splice_init(
                channel_id,
                amount,
                (psbt_to_base64(initial_psbt) if initial_psbt is not None else None),
                feerate_per_kw,
            )

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_init returned an invalid response")

            psbt = result.get("psbt")
            if not isinstance(psbt, str) or not psbt:
                raise RuntimeError("CLN splice_init response is missing psbt")

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to initiate channel splice: {exc}") from exc

    def splice_update(
        self,
        channel_id: str,
        psbt: bytes,
    ) -> dict[str, object]:
        """Update an active CLN channel splice."""
        try:
            result = self.rpc.splice_update(
                channel_id,
                psbt_to_base64(psbt),
            )

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_update returned an invalid response")

            returned_psbt = result.get("psbt")
            if not isinstance(returned_psbt, str) or not returned_psbt:
                raise RuntimeError("CLN splice_update response is missing psbt")

            commitments_secured = result.get("commitments_secured")
            if not isinstance(commitments_secured, bool):
                raise RuntimeError(
                    "CLN splice_update response has invalid commitments_secured"
                )

            if "signatures_secured" in result and not isinstance(
                result["signatures_secured"], bool
            ):
                raise RuntimeError(
                    "CLN splice_update response has invalid signatures_secured"
                )

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to update channel splice: {exc}") from exc

    def splice_signed(
        self,
        channel_id: str,
        psbt: bytes,
        sign_first: bool = False,
    ) -> dict[str, object]:
        """Complete an active CLN channel splice."""
        try:
            if sign_first:
                raise RuntimeError(
                    "pyln-client splice_signed wrapper does not support sign_first"
                )

            result = self.rpc.splice_signed(
                channel_id,
                psbt_to_base64(psbt),
            )

            if not isinstance(result, dict):
                raise RuntimeError("CLN splice_signed returned an invalid response")

            for field in ("tx", "txid", "psbt"):
                value = result.get(field)
                if not isinstance(value, str) or not value:
                    raise RuntimeError(f"CLN splice_signed response is missing {field}")

            outnum = result.get("outnum")
            if outnum is not None and (
                not isinstance(outnum, int) or isinstance(outnum, bool) or outnum < 0
            ):
                raise RuntimeError("CLN splice_signed response has invalid outnum")

            return dict(result)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to complete channel splice: {exc}") from exc

    def get_channel_funding_status(
        self,
        peer_id: str,
        txid: str,
    ) -> ChannelFundingStatus:
        """
        Determine the CLN funding state for the expected transaction.

        ``fundchannel_complete(withhold=True)`` records the channel and
        marks it withheld. ``sendpsbt`` clears that flag and associates
        the transaction with the channel. ``listtransactions`` provides
        a second source of truth for an already-broadcast transaction.
        """
        try:
            peer_result = self.rpc.listpeerchannels(peer_id)
            if not isinstance(peer_result, dict):
                raise RuntimeError("CLN listpeerchannels returned an invalid response")

            channels = peer_result.get("channels")
            if channels is None:
                channels = []
            elif not isinstance(channels, list):
                raise RuntimeError(
                    "CLN listpeerchannels returned invalid channels data"
                )

            for channel in channels:
                if not isinstance(channel, dict):
                    raise RuntimeError(
                        "CLN listpeerchannels returned an invalid channel entry"
                    )

                if channel.get("funding_txid") == txid:
                    funding = channel.get("funding")
                    if isinstance(funding, dict) and funding.get("withheld") is True:
                        return ChannelFundingStatus.WITHHELD

                    # A matching channel which is no longer withheld is
                    # deliberately treated as broadcast/released. This is
                    # the conservative state for cancellation and UTXO
                    # unlocking: never attempt to cancel or reuse inputs.
                    return ChannelFundingStatus.BROADCAST

                inflight = channel.get("inflight")
                if inflight is None:
                    continue
                if not isinstance(inflight, list):
                    raise RuntimeError(
                        "CLN listpeerchannels returned invalid inflight data"
                    )

                for candidate in inflight:
                    if not isinstance(candidate, dict):
                        raise RuntimeError(
                            "CLN listpeerchannels returned an invalid "
                            "inflight channel entry"
                        )
                    if candidate.get("funding_txid") == txid:
                        return ChannelFundingStatus.WITHHELD

            transaction_result = self.rpc.listtransactions()
            if not isinstance(transaction_result, dict):
                raise RuntimeError("CLN listtransactions returned an invalid response")

            transactions = transaction_result.get("transactions")
            if transactions is None:
                transactions = []
            elif not isinstance(transactions, list):
                raise RuntimeError(
                    "CLN listtransactions returned invalid transactions data"
                )

            for transaction in transactions:
                if not isinstance(transaction, dict):
                    raise RuntimeError(
                        "CLN listtransactions returned an invalid transaction entry"
                    )
                if transaction.get("hash") == txid:
                    return ChannelFundingStatus.BROADCAST

            return ChannelFundingStatus.ABSENT

        except Exception as exc:
            raise RuntimeError(
                f"Failed to determine CLN funding status for {peer_id}: {exc}"
            ) from exc

    def get_funding_start_status(self, peer_id: str) -> ChannelFundingStatus:
        """Check whether CLN still reports an in-flight channel open."""
        try:
            result = self.rpc.listpeerchannels(peer_id)
            if not isinstance(result, dict):
                raise RuntimeError("CLN listpeerchannels returned an invalid response")
            channels = result.get("channels", [])
            if not isinstance(channels, list):
                raise RuntimeError(
                    "CLN listpeerchannels returned invalid channels data"
                )
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                inflight = channel.get("inflight", [])
                if isinstance(inflight, list) and inflight:
                    return ChannelFundingStatus.WITHHELD
            return ChannelFundingStatus.ABSENT
        except Exception as exc:
            raise RuntimeError(
                f"Failed to determine CLN funding-start status for {peer_id}: {exc}"
            ) from exc

    def get_splice_funding_status(self, channel_id: str) -> ChannelFundingStatus:
        """Check the authoritative CLN state of an in-flight splice."""
        try:
            result = self.rpc.listpeerchannels(channel_id)
            if not isinstance(result, dict):
                raise RuntimeError("CLN listpeerchannels returned an invalid response")
            channels = result.get("channels", [])
            if not isinstance(channels, list):
                raise RuntimeError(
                    "CLN listpeerchannels returned invalid channels data"
                )
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                inflight = channel.get("inflight", [])
                if isinstance(inflight, list) and inflight:
                    return ChannelFundingStatus.WITHHELD
            return ChannelFundingStatus.ABSENT
        except Exception as exc:
            raise RuntimeError(
                f"Failed to determine CLN splice status for {channel_id}: {exc}"
            ) from exc

    def send_psbt(self, psbt: bytes) -> dict[str, object]:
        """Finalise and broadcast a fully signed PSBT through CLN."""
        try:
            result = self.rpc.sendpsbt(
                psbt=psbt_to_base64(psbt),
            )
            return dict(result)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to send funding PSBT through CLN: {exc}"
            ) from exc

    def get_splice_feerate_per_kw(self) -> int:
        """Return CLN's current splice feerate in sat/kw."""
        try:
            result = self.rpc.feerates(style="perkw")
        except Exception as exc:
            raise RuntimeError(f"Failed to retrieve CLN splice feerate: {exc}") from exc

        if not isinstance(result, dict):
            raise RuntimeError("CLN feerates returned an invalid response")

        perkw = result.get("perkw")
        if not isinstance(perkw, dict):
            raise RuntimeError("CLN feerates response is missing perkw data")

        splice = perkw.get("splice")
        if not isinstance(splice, int) or isinstance(splice, bool) or splice <= 0:
            raise RuntimeError("CLN feerates response has an invalid splice rate")

        return splice

    def get_fee_rate(
        self,
        priority: FeePriority = FeePriority.NORMAL,
        feerate: str | int | None = None,
    ) -> float:
        """Return a fee rate in sat/vbyte suitable for planner().

        When ``feerate`` is supplied, it is interpreted using CLN's native
        feerate syntax. Otherwise the configured fee priority is used.
        """
        if feerate is not None:
            try:
                result = self.rpc.parsefeerate(str(feerate))
            except Exception as exc:
                raise RuntimeError(f"Failed to parse CLN feerate: {exc}") from exc

            if not isinstance(result, dict):
                raise RuntimeError("CLN parsefeerate returned an invalid response")

            perkw = result.get("perkw")
            if not isinstance(perkw, int) or isinstance(perkw, bool) or perkw <= 0:
                raise RuntimeError("CLN parsefeerate returned an invalid fee rate")

            fee_rate = perkw / 250.0
            if not isfinite(fee_rate) or fee_rate <= 0:
                raise RuntimeError("CLN parsefeerate returned an invalid fee rate")

            return fee_rate

        estimates = self._estimate_fees()

        mapping = {
            FeePriority.HIGH: 2,
            FeePriority.NORMAL: 6,
            FeePriority.ECONOMY: 12,
        }

        requested = mapping[priority]

        estimate = min(
            estimates,
            key=lambda estimate: abs(estimate.blocks - requested),
        )

        fee_rate = estimate.sat_per_kvb / 1000.0

        if not isfinite(fee_rate) or fee_rate <= 0:
            raise RuntimeError("CLN returned an invalid fee rate")

        return fee_rate

    @property
    def funding_output_type(self) -> str:
        return "p2wsh"
