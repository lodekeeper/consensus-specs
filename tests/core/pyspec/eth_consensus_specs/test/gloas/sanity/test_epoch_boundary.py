from eth_consensus_specs.test.context import (
    spec_state_test,
    with_gloas_and_later,
    with_presets,
)
from eth_consensus_specs.test.helpers.block import build_empty_block
from eth_consensus_specs.test.helpers.consolidations import (
    prepare_switch_to_compounding_request,
)
from eth_consensus_specs.test.helpers.constants import MINIMAL
from eth_consensus_specs.test.helpers.state import (
    state_transition_and_sign_block,
)
from eth_consensus_specs.test.helpers.withdrawals import (
    set_eth1_withdrawal_credential_with_balance,
)
from tests.infra.helpers.withdrawals import set_parent_block_full


def _get_last_slot_of_current_epoch(spec, state):
    epoch = spec.get_current_epoch(state)
    return (epoch + 1) * spec.SLOTS_PER_EPOCH - 1


def _setup_switch_to_compounding_validator(spec, state, validator_index):
    """
    Set up a validator with ETH1 withdrawal credentials and balance above
    MIN_ACTIVATION_BALANCE, ready for a switch-to-compounding request.

    Returns the consolidation request.
    """
    address = b"\xaa" * 20
    set_eth1_withdrawal_credential_with_balance(
        spec, state, validator_index, address=address,
    )
    # Give the validator a balance above MIN_ACTIVATION_BALANCE so that
    # after switching to compounding, effective balance can increase.
    balance = spec.MIN_ACTIVATION_BALANCE + 3 * spec.EFFECTIVE_BALANCE_INCREMENT
    state.balances[validator_index] = balance

    consolidation_request = prepare_switch_to_compounding_request(
        spec, state, validator_index, address=address,
    )
    return consolidation_request


def _build_block_with_execution_requests(spec, state, slot, execution_requests):
    """
    Build a block at ``slot`` whose bid commits to ``execution_requests``.
    The bid uses self-build (BUILDER_INDEX_SELF_BUILD) so it can be signed
    with the proposer's key.
    """
    block = build_empty_block(spec, state, slot=slot)

    bid = block.body.signed_execution_payload_bid.message
    bid.execution_requests_root = spec.hash_tree_root(execution_requests)

    # Re-sign the bid (self-build uses G2_POINT_AT_INFINITY)
    if bid.builder_index == spec.BUILDER_INDEX_SELF_BUILD:
        block.body.signed_execution_payload_bid = spec.SignedExecutionPayloadBid(
            message=bid,
            signature=spec.G2_POINT_AT_INFINITY,
        )

    return block


def _build_child_block_with_parent_requests(spec, state, slot, parent_execution_requests):
    """
    Build a block at ``slot`` that carries ``parent_execution_requests``
    for the parent payload that was full.
    """
    block = build_empty_block(spec, state, slot=slot)
    block.body.parent_execution_requests = parent_execution_requests
    return block


def _run_epoch_boundary_full_parent(spec, state, gap_epochs):
    """
    Block at last slot of epoch with switch-to-compounding request in payload,
    payload delivered (full), then ``gap_epochs`` of missed blocks, then a new
    block that processes the execution requests from the parent payload.
    """
    set_parent_block_full(spec, state)

    # Pick a validator and set up for switch-to-compounding
    validator_index = 0
    consolidation_request = _setup_switch_to_compounding_validator(
        spec, state, validator_index,
    )

    execution_requests = spec.ExecutionRequests(
        consolidations=[consolidation_request],
    )

    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])

    yield "pre", state

    # Block 1: last slot of current epoch
    last_slot = _get_last_slot_of_current_epoch(spec, state)
    block_1 = _build_block_with_execution_requests(
        spec, state, last_slot, execution_requests,
    )
    signed_block_1 = state_transition_and_sign_block(spec, state, block_1)

    # Simulate payload delivery for Block 1
    set_parent_block_full(spec, state)

    # Validator should NOT have switched yet (processed by next block)
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])

    # Block 2: after gap_epochs of missed blocks
    block_1_epoch = spec.compute_epoch_at_slot(block_1.slot)
    block_2_slot = (block_1_epoch + gap_epochs) * spec.SLOTS_PER_EPOCH + 1
    block_2 = _build_child_block_with_parent_requests(
        spec, state, block_2_slot, execution_requests,
    )
    signed_block_2 = state_transition_and_sign_block(spec, state, block_2)

    yield "blocks", [signed_block_1, signed_block_2]
    yield "post", state

    # Switch-to-compounding should now have been applied
    assert spec.has_compounding_withdrawal_credential(state.validators[validator_index])

    # Excess balance above MIN_ACTIVATION_BALANCE was queued as a pending
    # deposit by queue_excess_active_balance. The remaining balance may be
    # at or slightly below MIN_ACTIVATION_BALANCE due to attestation penalties
    # accumulated during the gap of missed slots.
    assert state.balances[validator_index] <= spec.MIN_ACTIVATION_BALANCE


def _run_epoch_boundary_empty_parent(spec, state, gap_epochs):
    """
    Block at last slot of epoch with switch-to-compounding request committed
    in bid, but payload NOT delivered (empty parent), then ``gap_epochs`` of
    missed blocks, then a new block. The request should NOT be processed.
    """
    set_parent_block_full(spec, state)

    # Pick a validator and set up for switch-to-compounding
    validator_index = 0
    consolidation_request = _setup_switch_to_compounding_validator(
        spec, state, validator_index,
    )

    execution_requests = spec.ExecutionRequests(
        consolidations=[consolidation_request],
    )

    # Verify the validator has ETH1 credentials before the switch
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])

    yield "pre", state

    # Block 1: last slot of current epoch
    last_slot = _get_last_slot_of_current_epoch(spec, state)
    block_1 = _build_block_with_execution_requests(
        spec, state, last_slot, execution_requests,
    )
    signed_block_1 = state_transition_and_sign_block(spec, state, block_1)

    # Do NOT deliver payload, parent stays empty
    # Validator should NOT have switched
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])

    # Block 2: after gap_epochs of missed blocks
    block_1_epoch = spec.compute_epoch_at_slot(block_1.slot)
    block_2_slot = (block_1_epoch + gap_epochs) * spec.SLOTS_PER_EPOCH + 1
    # Parent was empty so parent_execution_requests must be empty
    block_2 = build_empty_block(spec, state, slot=block_2_slot)
    signed_block_2 = state_transition_and_sign_block(spec, state, block_2)

    yield "blocks", [signed_block_1, signed_block_2]
    yield "post", state

    # Switch-to-compounding was never processed, credentials unchanged
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_full_parent_gap_1_epoch(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding in payload.
    Payload delivered. 1 epoch of missed blocks. Next block processes
    the parent's execution requests.
    """
    yield from _run_epoch_boundary_full_parent(spec, state, gap_epochs=1)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_full_parent_gap_2_epochs(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding in payload.
    Payload delivered. 2 epochs of missed blocks. Next block processes
    the parent's execution requests.
    """
    yield from _run_epoch_boundary_full_parent(spec, state, gap_epochs=2)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_full_parent_gap_5_epochs(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding in payload.
    Payload delivered. 5 epochs of missed blocks. Next block processes
    the parent's execution requests.
    """
    yield from _run_epoch_boundary_full_parent(spec, state, gap_epochs=5)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_empty_parent_gap_1_epoch(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding committed in bid.
    Payload NOT delivered. 1 epoch of missed blocks. Request never processed.
    """
    yield from _run_epoch_boundary_empty_parent(spec, state, gap_epochs=1)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_empty_parent_gap_2_epochs(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding committed in bid.
    Payload NOT delivered. 2 epochs of missed blocks. Request never processed.
    """
    yield from _run_epoch_boundary_empty_parent(spec, state, gap_epochs=2)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="long gap requires many empty slots")
def test_epoch_boundary_empty_parent_gap_5_epochs(spec, state):
    """
    Block at last slot of epoch with switch-to-compounding committed in bid.
    Payload NOT delivered. 5 epochs of missed blocks. Request never processed.
    """
    yield from _run_epoch_boundary_empty_parent(spec, state, gap_epochs=5)


@with_gloas_and_later
@spec_state_test
@with_presets([MINIMAL], reason="epoch boundary timing requires controlled slots")
def test_switch_to_compounding_across_epoch_boundary(spec, state):
    """
    A validator with >32 ETH and 0x01 credentials submits a switch-to-compounding
    request in the last payload of an epoch.

    The request is processed by the next block, so the epoch transition runs
    WITHOUT the credential change and the effective balance stays at
    MIN_ACTIVATION_BALANCE (not jumping to the higher compounding cap).
    """
    set_parent_block_full(spec, state)

    # Set up validator: 0x01 credentials, balance > MIN_ACTIVATION_BALANCE
    validator_index = 0
    consolidation_request = _setup_switch_to_compounding_validator(
        spec, state, validator_index,
    )
    pre_balance = state.balances[validator_index]
    assert pre_balance > spec.MIN_ACTIVATION_BALANCE

    execution_requests = spec.ExecutionRequests(
        consolidations=[consolidation_request],
    )

    # Effective balance is capped at MIN_ACTIVATION_BALANCE for 0x01 validators
    assert state.validators[validator_index].effective_balance == spec.MIN_ACTIVATION_BALANCE
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])

    yield "pre", state

    # Block at last slot of epoch, bid commits to switch-to-compounding
    last_slot = _get_last_slot_of_current_epoch(spec, state)
    block_1 = _build_block_with_execution_requests(
        spec, state, last_slot, execution_requests,
    )
    signed_block_1 = state_transition_and_sign_block(spec, state, block_1)

    # Payload delivered
    set_parent_block_full(spec, state)

    # At this point we're at the last slot of the epoch.
    # The switch-to-compounding request is committed but NOT yet processed.
    # Credentials must still be 0x01.
    assert spec.has_eth1_withdrawal_credential(state.validators[validator_index])
    # Effective balance must still be capped at MIN_ACTIVATION_BALANCE
    assert state.validators[validator_index].effective_balance == spec.MIN_ACTIVATION_BALANCE

    # Block at first slot of next epoch, processes parent's execution requests
    block_2 = _build_child_block_with_parent_requests(
        spec, state, last_slot + 1, execution_requests,
    )
    signed_block_2 = state_transition_and_sign_block(spec, state, block_2)

    yield "blocks", [signed_block_1, signed_block_2]
    yield "post", state

    # The switch should now have been applied
    assert spec.has_compounding_withdrawal_credential(state.validators[validator_index])

    # The epoch transition (process_effective_balance_updates) ran BEFORE the
    # switch was applied, so effective_balance was computed with 0x01 credentials.
    # After the switch, balance was set to MIN_ACTIVATION_BALANCE by
    # queue_excess_active_balance, so effective_balance should remain at
    # MIN_ACTIVATION_BALANCE (not jump to the pre-switch higher balance).
    assert state.balances[validator_index] <= spec.MIN_ACTIVATION_BALANCE
