# TODO

## Lightning / JoinMarket integration

- [ ] Implement CLN `close_to` using a JoinMarket address.
- [ ] Implement splice sweep semantics (`amount=0`) end-to-end.
- [ ] Support jmlightning-to-CLN RPC over a TCP socket.
- [ ] Investigate Ring Change Channels and how they could fit into jmlightning.

## Architecture / maintenance

- [ ] Consolidate duplicated operation lifecycle code where practical.
- [ ] Simplify the `TxBuilder` compatibility layer where supported.
- [ ] Release engineering hardening.
