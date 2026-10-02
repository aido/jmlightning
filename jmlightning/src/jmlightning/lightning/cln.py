import secrets
from dataclasses import dataclass
from math import isfinite
from typing import NoReturn

from jmcore.bitcoin import (
    decode_varint,
    encode_varint,
    parse_transaction_bytes,
    psbt_to_base64,
)
from jmwallet.wallet.psbt import (
    PSBT_IN_NON_WITNESS_UTXO,
    PSBT_IN_PROPRIETARY,
    PSBT_IN_WITNESS_UTXO,
    ParsedPSBT,
    PSBTError,
    parse_psbt,
)
from pyln.client import LightningRpc

from jmlightning.lightning.backend import (
    AddPsbtOutputResult,
    ChannelFundingStatus,
    FeePriority,
    FundChannelCompleteResult,
    LightningBackend,
    SendPsbtResult,
    SpliceInitResult,
    SpliceSignedResult,
    SpliceUpdateResult,
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


def _raise_cln_error(operation: str, exc: Exception) -> NoReturn:
    """Translate an RPC/backend exception into the CLN-facing error type."""
    raise RuntimeError(f"{operation}: {exc}") from exc


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


def estimate_splice_out_fee(
    psbt: bytes,
    feerate_per_kw: int,
) -> tuple[int, int]:
    """Estimate the initiator fee for a channel-only splice-out.

    The splice-out PSBT contains the payout output but no inputs before
    ``splice_init``. CLN subsequently adds the existing 2-of-2 channel input
    and the new 2-of-2 channel funding output. The BOLT #3 channel input is
    387 wu and the P2WSH funding output is 172 wu. Both are paid for by the
    splice initiator together with the common transaction fields.
    """
    if feerate_per_kw <= 0:
        raise ValueError("Fee rate must be positive")
    try:
        parsed_psbt = parse_psbt(psbt)
    except PSBTError as exc:
        raise ValueError("Invalid splice-out PSBT") from exc

    input_weight = sum(
        _input_weight(parsed_psbt, index)
        for index in range(len(parsed_psbt.input_maps))
    )
    output_weight = sum(
        _output_weight(output.script) for output in parsed_psbt.transaction.outputs
    )
    # CLN's BOLT #3 2-of-2 channel input weighs 391 wu at the
    # maximum 73-byte signature size used by psbt_input_get_weight().
    channel_input_weight = 391
    channel_output_weight = _output_weight(b"\x00\x20" + b"\x00" * 32)
    output_count = len(parsed_psbt.transaction.outputs) + 1
    weight = (
        input_weight
        + channel_input_weight
        + output_weight
        + channel_output_weight
        + _core_weight(
            len(parsed_psbt.transaction.inputs) + 1,
            output_count,
        )
    )
    return (feerate_per_kw * weight) // 1000, weight


def estimate_splice_in_fee(
    input_types: list[str],
    feerate_per_kw: int,
    add_change_output: bool = True,
) -> tuple[int, int]:
    """Estimate the CLN initiator fee for a splice-in transaction.

    ``input_types`` describes the JoinMarket inputs contributed by this
    node. CLN also charges the initiator for the existing 2-of-2 channel
    input, the new P2WSH channel output and, when requested, the JoinMarket
    change output. This covers both fixed-amount splice-ins and sweeps.
    """
    if feerate_per_kw <= 0:
        raise ValueError("Fee rate must be positive")

    input_weights = {
        "p2wpkh": 271,
        "p2wsh": 387,
    }
    try:
        joinmarket_weight = sum(input_weights[input_type] for input_type in input_types)
    except KeyError as exc:
        raise ValueError(f"Unsupported splice input type: {exc.args[0]}") from exc

    channel_input_weight = 391
    channel_output_weight = _output_weight(b"\x00\x20" + b"\x00" * 32)
    output_count = 1
    output_weight = channel_output_weight
    if add_change_output:
        output_weight += 124
        output_count += 1

    weight = (
        channel_input_weight
        + joinmarket_weight
        + output_weight
        + _core_weight(len(input_types) + 1, output_count)
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
            _raise_cln_error("Failed to retrieve fee estimates", exc)

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
        close_to: str | None = None,
    ) -> str:
        """Ask CLN for the 2-of-2 multisig address to fund the channel."""
        try:
            result = self.rpc.fundchannel_start(
                peer_id,
                amount,
                announce=announce,
                **({"close_to": close_to} if close_to is not None else {}),
            )
            funding_address = result.get("funding_address")

            if not isinstance(funding_address, str):
                raise RuntimeError(
                    "CLN fundchannel_start response is missing funding_address"
                )

            if close_to is not None and not isinstance(result.get("close_to"), str):
                raise RuntimeError(
                    "CLN fundchannel_start did not negotiate the requested close_to"
                )

            return funding_address
        except RuntimeError:
            raise
        except Exception as exc:
            _raise_cln_error("Failed to start channel open", exc)

    def open_channel_complete(
        self,
        peer_id: str,
        psbt: bytes,
    ) -> FundChannelCompleteResult:
        """Complete channel establishment using the funding transaction PSBT."""
        try:
            result = self.rpc.fundchannel_complete(
                node_id=peer_id,
                psbt=psbt_to_base64(psbt),
                withhold=True,
            )
            if not isinstance(result, dict):
                raise RuntimeError(
                    "CLN fundchannel_complete returned an invalid response"
                )
            channel_id = result.get("channel_id")
            commitments_secured = result.get("commitments_secured")
            if channel_id is not None and (
                not isinstance(channel_id, str) or not channel_id
            ):
                raise RuntimeError(
                    "CLN fundchannel_complete response has invalid channel_id"
                )
            if commitments_secured is not True:
                raise RuntimeError(
                    "CLN fundchannel_complete response has invalid commitments_secured"
                )
            typed_result: FundChannelCompleteResult = {
                "commitments_secured": commitments_secured,
            }
            if channel_id is not None:
                typed_result["channel_id"] = channel_id
            return typed_result
        except Exception as exc:
            _raise_cln_error("Failed to complete channel open", exc)

    def cancel_channel_funding(
        self,
        peer_id: str,
    ) -> None:
        """Cancel a funding operation before its funding transaction is broadcast."""
        try:
            self.rpc.fundchannel_cancel(node_id=peer_id)
        except Exception as exc:
            _raise_cln_error("Failed to cancel channel funding", exc)

    def splice_init(
        self,
        channel_id: str,
        amount: int,
        initial_psbt: bytes | None = None,
        feerate_per_kw: int | None = None,
        force_feerate: bool = False,
    ) -> SpliceInitResult:
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

            return {"psbt": psbt}
        except RuntimeError:
            raise
        except Exception as exc:
            _raise_cln_error("Failed to initiate channel splice", exc)

    def add_psbt_output(
        self,
        amount: int,
        destination: str,
        initial_psbt: bytes | None = None,
    ) -> AddPsbtOutputResult:
        """Add a single output to a PSBT using CLN's wallet.

        ``addpsbtoutput`` is used for splice-out because CLN must assign the
        output its interactive transaction serial ID.
        """
        if amount <= 0:
            raise ValueError("PSBT output amount must be positive")
        if not destination:
            raise ValueError("PSBT output destination must not be empty")

        try:
            result = self.rpc.addpsbtoutput(
                satoshi=amount,
                initialpsbt=(
                    psbt_to_base64(initial_psbt) if initial_psbt is not None else None
                ),
                destination=destination,
            )
            if not isinstance(result, dict):
                raise RuntimeError("CLN addpsbtoutput returned an invalid response")
            psbt = result.get("psbt")
            if not isinstance(psbt, str) or not psbt:
                raise RuntimeError("CLN addpsbtoutput response is missing psbt")
            estimated_added_weight = result.get("estimated_added_weight")
            outnum = result.get("outnum")
            if (
                not isinstance(estimated_added_weight, int)
                or isinstance(estimated_added_weight, bool)
                or estimated_added_weight < 0
            ):
                raise RuntimeError(
                    "CLN addpsbtoutput response has invalid estimated_added_weight"
                )
            if not isinstance(outnum, int) or isinstance(outnum, bool) or outnum < 0:
                raise RuntimeError("CLN addpsbtoutput response has invalid outnum")
            return {
                "psbt": psbt,
                "estimated_added_weight": estimated_added_weight,
                "outnum": outnum,
            }
        except (RuntimeError, ValueError):
            raise
        except Exception as exc:
            _raise_cln_error("Failed to add splice output", exc)

    def splice_update(
        self,
        channel_id: str,
        psbt: bytes,
    ) -> SpliceUpdateResult:
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

            signatures_secured = result.get("signatures_secured")
            if signatures_secured is not None and not isinstance(
                signatures_secured, bool
            ):
                raise RuntimeError(
                    "CLN splice_update response has invalid signatures_secured"
                )

            typed_result: SpliceUpdateResult = {
                "psbt": returned_psbt,
                "commitments_secured": commitments_secured,
            }
            if signatures_secured is not None:
                typed_result["signatures_secured"] = signatures_secured
            return typed_result
        except RuntimeError:
            raise
        except Exception as exc:
            _raise_cln_error("Failed to update channel splice", exc)

    def splice_signed(
        self,
        channel_id: str,
        psbt: bytes,
        sign_first: bool = False,
    ) -> SpliceSignedResult:
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

            typed_result: SpliceSignedResult = {
                "tx": result["tx"],
                "txid": result["txid"],
                "psbt": result["psbt"],
            }
            if outnum is not None:
                typed_result["outnum"] = outnum
            return typed_result
        except RuntimeError:
            raise
        except Exception as exc:
            _raise_cln_error("Failed to complete channel splice", exc)

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
            _raise_cln_error(
                f"Failed to determine CLN funding status for {peer_id}", exc
            )

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
            _raise_cln_error(
                f"Failed to determine CLN funding-start status for {peer_id}", exc
            )

    def get_splice_funding_status(self, channel_id: str) -> ChannelFundingStatus:
        """Check the authoritative CLN state of an in-flight splice."""
        try:
            result = self.rpc.listpeerchannels(channel_id=channel_id)
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
            _raise_cln_error(
                f"Failed to determine CLN splice status for {channel_id}", exc
            )

    def send_psbt(self, psbt: bytes) -> SendPsbtResult:
        """Finalise and broadcast a fully signed PSBT through CLN."""
        try:
            result = self.rpc.sendpsbt(
                psbt=psbt_to_base64(psbt),
            )
            if not isinstance(result, dict):
                raise RuntimeError("CLN sendpsbt returned an invalid response")
            tx = result.get("tx")
            txid = result.get("txid")
            if not isinstance(tx, str) or not tx:
                raise RuntimeError("CLN sendpsbt response is missing tx")
            if not isinstance(txid, str) or not txid:
                raise RuntimeError("CLN sendpsbt response is missing txid")
            return {"tx": tx, "txid": txid}
        except Exception as exc:
            _raise_cln_error("Failed to send funding PSBT through CLN", exc)

    def get_channel_local_balance_sat(self, channel_id: str) -> int:
        """Return the channel balance currently owed to this node in sats.

        ``to_us_msat`` is CLN's authoritative channel balance field. A splice
        amount is denominated in whole satoshis, so any sub-satoshi remainder
        is deliberately rounded down.
        """
        try:
            result = self.rpc.listpeerchannels(channel_id=channel_id)
        except Exception as exc:
            _raise_cln_error(
                f"Failed to retrieve channel balance for {channel_id}", exc
            )

        if not isinstance(result, dict):
            raise RuntimeError("CLN listpeerchannels returned an invalid response")
        channels = result.get("channels")
        if not isinstance(channels, list):
            raise RuntimeError("CLN listpeerchannels returned invalid channels data")

        for channel in channels:
            if not isinstance(channel, dict):
                continue
            if channel.get("channel_id") not in (None, channel_id):
                continue
            value = channel.get("to_us_msat")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(
                    "CLN listpeerchannels returned an invalid to_us_msat value"
                )
            return value // 1000

        raise RuntimeError(f"CLN channel {channel_id} was not found")

    def get_channel_capacity_sat(self, channel_id: str) -> int:
        """Return the channel capacity in sats."""
        try:
            result = self.rpc.listpeerchannels(channel_id=channel_id)
        except Exception as exc:
            _raise_cln_error(
                f"Failed to retrieve channel capacity for {channel_id}", exc
            )

        if not isinstance(result, dict) or not isinstance(result.get("channels"), list):
            raise RuntimeError("CLN listpeerchannels returned invalid channel data")

        for channel in result["channels"]:
            if not isinstance(channel, dict):
                continue
            if channel.get("channel_id") not in (None, channel_id):
                continue
            value = channel.get("amount_msat")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(
                    "CLN listpeerchannels returned an invalid amount_msat value"
                )
            return value // 1000

        raise RuntimeError(f"CLN channel {channel_id} was not found")

    def get_splice_feerate_per_kw(self) -> int:
        """Return CLN's current splice feerate in sat/kw."""
        try:
            result = self.rpc.feerates(style="perkw")
        except Exception as exc:
            _raise_cln_error("Failed to retrieve CLN splice feerate", exc)

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
                _raise_cln_error("Failed to parse CLN feerate", exc)

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
