"""
Tests for withdrawal liability reservation across epoch boundaries.

These tests exercise the attack vectors that the liability mechanism prevents:
1. Consolidation draining source balance to 0 between commitment and deduction
2. Slashing/penalties reducing balance below committed withdrawal amount
"""
from eth_consensus_specs.test.context import (
    spec_state_test,
    with_gloas_and_later,
)
from eth_consensus_specs.test.helpers.epoch_processing import (
    run_epoch_processing_with,
)
from eth_consensus_specs.test.helpers.withdrawals import (
    set_eth1_withdrawal_credential_with_balance,
)


@with_gloas_and_later
@spec_state_test
def test_consolidation_cannot_drain_reserved_withdrawal_balance(spec, state):
    """
    The core attack vector: a pending consolidation tries to move a source
    validator's entire balance at an epoch boundary, but the validator also has
    a committed withdrawal in `payload_expected_withdrawals`. The reserve floor
    on `decrease_balance` must prevent the consolidation from draining the
    reserved amount.
    """
    current_epoch = spec.get_current_epoch(state)
    source_index = spec.get_active_validator_indices(state, current_epoch)[0]
    target_index = spec.get_active_validator_indices(state, current_epoch)[1]

    source_balance = state.balances[source_index]
    reserved_amount = spec.Gwei(source_balance // 2)  # half the balance is reserved

    # Set up source as withdrawable (required for consolidation processing)
    state.validators[source_index].withdrawable_epoch = current_epoch
    address = b"\xaa" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, source_index, address=address)

    # Set up target with eth1 credentials
    target_address = b"\xbb" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, target_index, address=target_address)

    # Commit a withdrawal liability for the source validator
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=source_index,
            address=spec.ExecutionAddress(address),
            amount=reserved_amount,
        )
    ]

    # Queue a pending consolidation that would normally drain source to 0
    state.pending_consolidations.append(
        spec.PendingConsolidation(source_index=source_index, target_index=target_index)
    )

    pre_source_balance = state.balances[source_index]
    pre_target_balance = state.balances[target_index]

    yield from run_epoch_processing_with(spec, state, "process_pending_consolidations")

    # Source balance must NOT drop below the reserved amount
    assert state.balances[source_index] >= reserved_amount, (
        f"source balance {state.balances[source_index]} dropped below "
        f"reserved amount {reserved_amount}"
    )
    # The consolidation should only move the spendable portion
    spendable = pre_source_balance - reserved_amount
    moved = min(spendable, state.validators[source_index].effective_balance)
    assert state.balances[target_index] == pre_target_balance + moved, (
        f"target balance {state.balances[target_index]} != "
        f"expected {pre_target_balance + moved}"
    )
    # Consolidation was still processed (not skipped)
    assert state.pending_consolidations == []


@with_gloas_and_later
@spec_state_test
def test_penalties_cannot_eat_into_reserved_withdrawal_balance(spec, state):
    """
    Verify that process_slashings (called during epoch processing) cannot
    reduce a validator's balance below the reserved withdrawal amount.
    """
    current_epoch = spec.get_current_epoch(state)
    validator_index = spec.get_active_validator_indices(state, current_epoch)[0]

    address = b"\xcc" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, validator_index, address=address)

    reserved_amount = spec.Gwei(spec.EFFECTIVE_BALANCE_INCREMENT)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(address),
            amount=reserved_amount,
        )
    ]

    # Slash the validator so process_slashings will apply a penalty
    state.validators[validator_index].slashed = True
    state.validators[validator_index].withdrawable_epoch = (
        current_epoch + spec.EPOCHS_PER_SLASHINGS_VECTOR
    )
    state.slashings[current_epoch % spec.EPOCHS_PER_SLASHINGS_VECTOR] = (
        state.validators[validator_index].effective_balance
    )
    # Make the slashing penalty fire this epoch
    state.validators[validator_index].withdrawable_epoch = (
        current_epoch + spec.EPOCHS_PER_SLASHINGS_VECTOR // 2
    )

    yield from run_epoch_processing_with(spec, state, "process_slashings")

    # Balance must not drop below reserved amount
    assert state.balances[validator_index] >= reserved_amount, (
        f"balance {state.balances[validator_index]} dropped below "
        f"reserved amount {reserved_amount}"
    )


@with_gloas_and_later
@spec_state_test
def test_effective_balance_not_distorted_by_reserved_withdrawal(spec, state):
    """
    Verify that process_effective_balance_updates uses the raw balance
    (not spendable balance), so effective_balance is not artificially
    reduced during the liability window.
    """
    current_epoch = spec.get_current_epoch(state)
    validator_index = spec.get_active_validator_indices(state, current_epoch)[0]

    address = b"\xdd" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, validator_index, address=address)

    # Set balance to exactly MIN_ACTIVATION_BALANCE
    state.balances[validator_index] = spec.MIN_ACTIVATION_BALANCE
    state.validators[validator_index].effective_balance = spec.MIN_ACTIVATION_BALANCE

    # Reserve a small amount — if effective_balance used spendable balance,
    # it would see balance < effective_balance and trigger a downward adjustment
    reserved_amount = spec.Gwei(spec.EFFECTIVE_BALANCE_INCREMENT)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(address),
            amount=reserved_amount,
        )
    ]

    yield from run_epoch_processing_with(spec, state, "process_effective_balance_updates")

    # effective_balance should NOT have been reduced
    assert state.validators[validator_index].effective_balance == spec.MIN_ACTIVATION_BALANCE, (
        f"effective_balance was incorrectly reduced to "
        f"{state.validators[validator_index].effective_balance} "
        f"during liability window"
    )
