# Contributing

Thank you for helping. Before a pull request can be merged, everyone who wrote part of it
must have signed the Pandora-On-Zero Contributor License Agreement ([CLA.md](CLA.md)). It
lets Pandora-On-Zero license the project, your contribution included, under its licence and
under separate commercial terms.

Signing takes one comment: on your first pull request a bot asks you to sign, and you reply
with

> I have read the CLA Document and I hereby sign the CLA

You sign once; it covers all your contributions.

## One set of operations in every language

Reading a receiver without the app (`open_receiver()`) is pandora-core-rs's runtime, which
Rust, C and C++ programs call too, and a program should be able to do the same from any of
them. So a new operation on `Local` comes with the same operation in pandora-core-rs's
runtime and in each of its languages (its `tools/check_api_parity.py` lists them), and a
new operation in pandora-core-rs comes here. `tests/test_api_parity.py` fails on either gap.

Run the tests with `pip install -e ".[dev]"` and `pytest`.
