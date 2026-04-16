from eth_consensus_specs.test.context import (
    default_activation_threshold,
    expect_assertion_error,
    scaled_churn_balances_exceed_activation_exit_churn_limit,
    spec_state_test,
    spec_test,
    with_custom_state,
    with_gloas_and_later,
    with_presets,
)
from eth_consensus_specs.test.helpers.block import build_empty_block_for_next_slot
from eth_consensus_specs.test.helpers.constants import MINIMAL
from eth_consensus_specs.test.helpers.execution_requests import (
    get_non_empty_execution_requests,
)
from eth_consensus_specs.test.helpers.withdrawals import (
    set_compounding_withdrawal_credential,
    set_eth1_withdrawal_credential_with_balance,
)
from tests.infra.helpers.withdrawals import set_parent_block_full


def run_parent_execution_payload_processing(spec, state, block, valid=True):
    """
    Run ``process_parent_execution_payload`` against a prepared pre-state.
    """
    yield "pre", state
    yield "block", block

    if not valid:
        expect_assertion_error(lambda: spec.process_parent_execution_payload(state, block))
        yield "post", None
        return

    spec.process_parent_execution_payload(state, block)
    yield "post", state


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload__empty_parent(spec, state):
    """
    Test that process_parent_execution_payload returns early when the parent
    block was empty (payload not delivered).
    """
    block = build_empty_block_for_next_slot(spec, state)

    is_parent_block_full = (
        block.body.signed_execution_payload_bid.message.parent_block_hash
        == state.latest_execution_payload_bid.block_hash
    )
    assert not is_parent_block_full

    pre_latest_block_hash = state.latest_block_hash
    parent_slot = state.latest_execution_payload_bid.slot
    pre_availability = state.execution_payload_availability[
        parent_slot % spec.SLOTS_PER_HISTORICAL_ROOT
    ]

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state.latest_block_hash == pre_latest_block_hash
    assert (
        state.execution_payload_availability[parent_slot % spec.SLOTS_PER_HISTORICAL_ROOT]
        == pre_availability
    )


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload__full_parent(spec, state):
    """
    Test that process_parent_execution_payload processes the parent's execution
    requests and updates state when the parent block was full.
    """
    set_parent_block_full(spec, state)
    block = build_empty_block_for_next_slot(spec, state)

    parent_bid = state.latest_execution_payload_bid.copy()
    parent_slot_index = parent_bid.slot % spec.SLOTS_PER_HISTORICAL_ROOT
    state.execution_payload_availability[parent_slot_index] = 0b0

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state.latest_block_hash == parent_bid.block_hash
    assert state.execution_payload_availability[parent_slot_index] == 0b1


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload__empty_parent_requires_empty_requests(spec, state):
    """
    Test that when parent is empty, parent_execution_requests must be empty.
    """
    block = build_empty_block_for_next_slot(spec, state)

    is_parent_block_full = (
        block.body.signed_execution_payload_bid.message.parent_block_hash
        == state.latest_execution_payload_bid.block_hash
    )
    assert not is_parent_block_full

    block.body.parent_execution_requests = get_non_empty_execution_requests(spec)

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block, valid=False)


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload__clears_liability_before_parent_withdrawal_request(spec, state):
    state.slot += spec.config.SHARD_COMMITTEE_PERIOD * spec.SLOTS_PER_EPOCH
    current_epoch = spec.get_current_epoch(state)
    validator_index = spec.get_active_validator_indices(state, current_epoch)[0]
    validator_pubkey = state.validators[validator_index].pubkey
    address = b"\x44" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, validator_index, address=address)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(address),
            amount=spec.Gwei(1),
        )
    ]
    set_parent_block_full(spec, state)
    block = build_empty_block_for_next_slot(spec, state)
    block.body.parent_execution_requests = spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](
            [
                spec.WithdrawalRequest(
                    source_address=address,
                    validator_pubkey=validator_pubkey,
                    amount=spec.FULL_EXIT_REQUEST_AMOUNT,
                )
            ]
        ),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](),
    )
    state.latest_execution_payload_bid.execution_requests_root = spec.hash_tree_root(
        block.body.parent_execution_requests
    )

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state.payload_expected_withdrawals == []
    assert state.validators[validator_index].exit_epoch < spec.FAR_FUTURE_EPOCH


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload__clears_liability_before_parent_partial_withdrawal_request(spec, state):
    state.slot += spec.config.SHARD_COMMITTEE_PERIOD * spec.SLOTS_PER_EPOCH
    current_epoch = spec.get_current_epoch(state)
    validator_index = spec.get_active_validator_indices(state, current_epoch)[0]
    validator_pubkey = state.validators[validator_index].pubkey
    address = b"\x55" * 20
    amount = spec.EFFECTIVE_BALANCE_INCREMENT
    state.balances[validator_index] += amount
    set_compounding_withdrawal_credential(
        spec,
        state,
        validator_index,
        address=address,
    )
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=validator_index,
            address=spec.ExecutionAddress(address),
            amount=spec.Gwei(1),
        )
    ]
    set_parent_block_full(spec, state)
    block = build_empty_block_for_next_slot(spec, state)
    block.body.parent_execution_requests = spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](
            [
                spec.WithdrawalRequest(
                    source_address=address,
                    validator_pubkey=validator_pubkey,
                    amount=amount,
                )
            ]
        ),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](),
    )
    state.latest_execution_payload_bid.execution_requests_root = spec.hash_tree_root(
        block.body.parent_execution_requests
    )

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state.payload_expected_withdrawals == []
    assert len(state.pending_partial_withdrawals) == 1
    assert state.pending_partial_withdrawals[0].validator_index == validator_index


@with_gloas_and_later
@with_presets([MINIMAL], "need sufficient consolidation churn limit")
@with_custom_state(
    balances_fn=scaled_churn_balances_exceed_activation_exit_churn_limit,
    threshold_fn=default_activation_threshold,
)
@spec_test
def test_process_parent_execution_payload__clears_liability_before_parent_consolidation_request(spec, state, phases=None):
    state.slot += spec.config.SHARD_COMMITTEE_PERIOD * spec.SLOTS_PER_EPOCH
    current_epoch = spec.get_current_epoch(state)
    source_index = spec.get_active_validator_indices(state, current_epoch)[0]
    target_index = spec.get_active_validator_indices(state, current_epoch)[1]
    source_address = b"\x66" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, source_index, address=source_address)
    set_compounding_withdrawal_credential(spec, state, target_index)
    state.earliest_consolidation_epoch = spec.compute_activation_exit_epoch(current_epoch)
    state.consolidation_balance_to_consume = spec.get_consolidation_churn_limit(state)
    state.payload_expected_withdrawals = [
        spec.Withdrawal(
            index=spec.WithdrawalIndex(0),
            validator_index=source_index,
            address=spec.ExecutionAddress(source_address),
            amount=spec.Gwei(1),
        )
    ]
    set_parent_block_full(spec, state)
    block = build_empty_block_for_next_slot(spec, state)
    block.body.parent_execution_requests = spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](
            [
                spec.ConsolidationRequest(
                    source_address=source_address,
                    source_pubkey=state.validators[source_index].pubkey,
                    target_pubkey=state.validators[target_index].pubkey,
                )
            ]
        ),
    )
    state.latest_execution_payload_bid.execution_requests_root = spec.hash_tree_root(
        block.body.parent_execution_requests
    )

    spec.process_slots(state, block.slot)
    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state.payload_expected_withdrawals == []
    assert len(state.pending_consolidations) == 1
    assert state.pending_consolidations[0].source_index == source_index
    assert state.pending_consolidations[0].target_index == target_index
