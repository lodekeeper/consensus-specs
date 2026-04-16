from eth_consensus_specs.test.context import spec_state_test, with_gloas_and_later


@with_gloas_and_later
@spec_state_test
def test_payload_expected_withdrawals_contribute_to_pending_balance(spec, state):
    validator_index = spec.ValidatorIndex(0)
    amount = spec.Gwei(11)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(b"\x11" * 20),
            amount=amount,
        )
    ]

    yield "pre", state
    assert spec.get_pending_balance_to_withdraw(state, validator_index) == amount
    yield "post", state


@with_gloas_and_later
@spec_state_test
def test_payload_expected_withdrawals_reserve_balance_until_settlement(spec, state):
    validator_index = spec.ValidatorIndex(0)
    reserved = spec.Gwei(10)
    state.balances[validator_index] = spec.Gwei(40)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(b"\x22" * 20),
            amount=reserved,
        )
    ]

    yield "pre", state

    spec.decrease_balance(state, validator_index, spec.Gwei(35))
    assert state.balances[validator_index] == reserved

    spec.apply_withdrawals(state, state.payload_expected_withdrawals)
    assert state.balances[validator_index] == spec.Gwei(0)

    yield "post", state


@with_gloas_and_later
@spec_state_test
def test_builder_bid_cannot_spend_payload_expected_withdrawals(spec, state):
    builder_index = spec.BuilderIndex(0)
    reserved = spec.Gwei(15)
    bid_amount = spec.Gwei(6)

    state.builders[builder_index].balance = spec.MIN_DEPOSIT_AMOUNT + reserved + spec.Gwei(5)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=spec.convert_builder_index_to_validator_index(builder_index),
            address=spec.ExecutionAddress(b"\x33" * 20),
            amount=reserved,
        )
    ]

    yield "pre", state

    assert spec.get_pending_balance_to_withdraw_for_builder(state, builder_index) == reserved
    assert not spec.can_builder_cover_bid(state, builder_index, bid_amount)

    yield "post", state
