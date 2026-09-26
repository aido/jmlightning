# TODO

## Lightning / JoinMarket integration

- [ ] Implement CLN `close_to` using a JoinMarket address.
- [ ] Implement splice sweep semantics (`amount=0`) end-to-end.
- [ ] Support jmlightning-to-CLN RPC over a TCP socket.
- [ ] Investigate Ring Change Channels and how they could fit into jmlightning.

## Architecture / maintenance

- [x] Consolidate duplicated operation lifecycle code where practical.
- [x] Simplify the `TxBuilder` compatibility layer where supported.
- [ ] Release engineering hardening.
- [ ] Remove workaround for lack of PSBTv2 (BIP370) support in jmwallet (see joinmarket-ng/joinmarket-ng#634)
- [ ] Remove workaround for jmwallet interpreting every P2WSH input as a JoinMarket fidelity bond (see joinmarket-ng/joinmarket-ng#635)
