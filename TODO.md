# TODO

## Lightning / JoinMarket integration

- [ ] Implement CLN `close_to` using a JoinMarket address.
- [ ] Implement splice sweep semantics (`amount=0`) end-to-end.
- [ ] Implement splice-out
- [ ] Support jmlightning-to-CLN RPC over a TCP socket.
- [ ] Investigate Ring Change Channels and how they could fit into jmlightning (see https://gist.github.com/m0wer/ce321e9622b5a3d32c71f56fa2a80ff5).

## Architecture / maintenance

- [x] Consolidate duplicated operation lifecycle code where practical.
- [x] Simplify the `TxBuilder` compatibility layer where supported.
- [ ] Release engineering hardening.
- [ ] Remove workaround for lack of PSBTv2 (BIP370) support in jmwallet (see joinmarket-ng/joinmarket-ng#634).
- [x] Remove workaround for jmwallet interpreting every P2WSH input as a JoinMarket fidelity bond (see joinmarket-ng/joinmarket-ng#635).
