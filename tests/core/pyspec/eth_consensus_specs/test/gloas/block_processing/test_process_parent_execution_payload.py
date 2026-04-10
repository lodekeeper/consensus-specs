from eth_consensus_specs.test.context import (
    always_bls,
    default_activation_threshold,
    expect_assertion_error,
    scaled_churn_balances_exceed_activation_exit_churn_limit,
    spec_state_test,
    spec_test,
    with_gloas_and_later,
    with_presets,
)
from eth_consensus_specs.test.helpers.constants import MINIMAL
from eth_consensus_specs.test.helpers.deposits import (
    make_withdrawal_credentials,
    prepare_deposit_request,
)
from eth_consensus_specs.test.helpers.withdrawals import (
    prepare_withdrawal_request,
    set_compounding_withdrawal_credential_with_balance,
    set_eth1_withdrawal_credential_with_balance,
)
from tests.core.pyspec.eth_consensus_specs.test.helpers.genesis import create_genesis_state


def _make_hash(spec, byte):
    return spec.Hash32(bytes([byte]) * 32)


def _build_execution_requests(
    spec,
    deposits=None,
    withdrawals=None,
    consolidations=None,
):
    return spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](
            deposits or []
        ),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](
            withdrawals or []
        ),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](consolidations or []),
    )


def _setup_parent_payload_state(
    spec,
    state,
    parent_slot,
    builder_index=0,
    value=None,
):
    if value is None:
        value = spec.Gwei(0)

    state.slot = parent_slot
    state.latest_block_header.slot = parent_slot
    state.latest_block_hash = _make_hash(spec, 0x11)

    parent_bid = spec.ExecutionPayloadBid(
        parent_block_hash=state.latest_block_hash,
        parent_block_root=state.latest_block_header.hash_tree_root(),
        block_hash=_make_hash(spec, 0x22),
        prev_randao=spec.get_randao_mix(state, spec.get_current_epoch(state)),
        fee_recipient=spec.ExecutionAddress(b"\xaa" * 20),
        gas_limit=spec.uint64(60_000_000),
        builder_index=builder_index,
        slot=parent_slot,
        value=value,
        blob_kzg_commitments=spec.List[spec.KZGCommitment, spec.MAX_BLOB_COMMITMENTS_PER_BLOCK](),
    )
    state.latest_execution_payload_bid = parent_bid

    if value > 0:
        payment_index = spec.SLOTS_PER_EPOCH + parent_slot % spec.SLOTS_PER_EPOCH
        state.builder_pending_payments[payment_index] = spec.BuilderPendingPayment(
            weight=0,
            withdrawal=spec.BuilderPendingWithdrawal(
                fee_recipient=parent_bid.fee_recipient,
                amount=value,
                builder_index=builder_index,
            ),
        )

    return parent_bid


def _rotate_builder_pending_payments(spec, state):
    old_payments = state.builder_pending_payments[spec.SLOTS_PER_EPOCH :]
    new_payments = [spec.BuilderPendingPayment() for _ in range(spec.SLOTS_PER_EPOCH)]
    state.builder_pending_payments = old_payments + new_payments


def _build_block_for_parent_processing(
    spec,
    state,
    slot,
    parent_bid,
    parent_execution_requests=None,
    parent_full=True,
):
    if parent_execution_requests is None:
        parent_execution_requests = spec.ExecutionRequests()

    child_bid = spec.ExecutionPayloadBid(
        parent_block_hash=(parent_bid.block_hash if parent_full else parent_bid.parent_block_hash),
        parent_block_root=state.latest_block_header.hash_tree_root(),
        block_hash=_make_hash(spec, 0x33),
        prev_randao=spec.get_randao_mix(state, spec.get_current_epoch(state)),
        fee_recipient=spec.ExecutionAddress(),
        gas_limit=spec.uint64(60_000_000),
        builder_index=spec.BUILDER_INDEX_SELF_BUILD,
        slot=slot,
        value=spec.Gwei(0),
        blob_kzg_commitments=spec.List[spec.KZGCommitment, spec.MAX_BLOB_COMMITMENTS_PER_BLOCK](),
    )

    block = spec.BeaconBlock(slot=slot)
    block.body.signed_execution_payload_bid = spec.SignedExecutionPayloadBid(
        message=child_bid,
        signature=spec.G2_POINT_AT_INFINITY,
    )
    block.body.parent_execution_requests = parent_execution_requests
    return block


def run_parent_execution_payload_processing(spec, state, block, valid=True):
    """
    Run ``process_parent_execution_payload``, yielding:
    - pre-state ('pre')
    - block ('block')
    - post-state ('post').
    If ``valid == False``, run expecting ``AssertionError``
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
@with_presets([MINIMAL], "need sufficient consolidation churn limit")
@spec_test
@always_bls
def test_process_parent_execution_payload_full_parent(spec, phases):
    state = create_genesis_state(
        spec,
        scaled_churn_balances_exceed_activation_exit_churn_limit(spec),
        default_activation_threshold(spec),
    )
    active_epoch = spec.Epoch(spec.config.SHARD_COMMITTEE_PERIOD + 2)
    parent_slot = spec.compute_start_slot_at_epoch(active_epoch) + 1
    child_slot = parent_slot + 1
    builder_index = 0
    payment_amount = spec.Gwei(5_000_000)

    parent_bid = _setup_parent_payload_state(
        spec, state, parent_slot, builder_index=builder_index, value=payment_amount
    )
    state.slot = child_slot

    for validator_index in (0, 1, 2):
        state.validators[validator_index].activation_epoch = spec.Epoch(0)
        state.validators[validator_index].exit_epoch = spec.FAR_FUTURE_EPOCH

    deposit_request = prepare_deposit_request(
        spec,
        len(state.validators),
        spec.MIN_DEPOSIT_AMOUNT,
        index=0,
        withdrawal_credentials=make_withdrawal_credentials(
            spec, spec.ETH1_ADDRESS_WITHDRAWAL_PREFIX, b"\x44"
        ),
        signed=True,
    )
    withdrawal_request = prepare_withdrawal_request(spec, state, 0)

    source_address = b"\x12" * 20
    set_eth1_withdrawal_credential_with_balance(spec, state, 1, address=source_address)
    set_compounding_withdrawal_credential_with_balance(spec, state, 2, address=b"\x34" * 20)
    state.earliest_consolidation_epoch = spec.compute_activation_exit_epoch(
        spec.get_current_epoch(state)
    )
    state.consolidation_balance_to_consume = spec.get_consolidation_churn_limit(state)
    consolidation_request = spec.ConsolidationRequest(
        source_address=spec.ExecutionAddress(source_address),
        source_pubkey=state.validators[1].pubkey,
        target_pubkey=state.validators[2].pubkey,
    )

    execution_requests = _build_execution_requests(
        spec,
        deposits=[deposit_request],
        withdrawals=[withdrawal_request],
        consolidations=[consolidation_request],
    )
    parent_bid = state.latest_execution_payload_bid
    block = _build_block_for_parent_processing(
        spec,
        state,
        child_slot,
        parent_bid,
        parent_execution_requests=execution_requests,
        parent_full=True,
    )

    payment_index = spec.SLOTS_PER_EPOCH + parent_slot % spec.SLOTS_PER_EPOCH
    pre_pending_deposits_len = len(state.pending_deposits)
    pre_pending_consolidations_len = len(state.pending_consolidations)
    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert len(state.pending_deposits) == pre_pending_deposits_len + 1
    pending_deposit = state.pending_deposits[pre_pending_deposits_len]
    assert pending_deposit.pubkey == deposit_request.pubkey
    assert pending_deposit.withdrawal_credentials == deposit_request.withdrawal_credentials
    assert pending_deposit.amount == deposit_request.amount
    assert pending_deposit.slot == child_slot

    assert state.validators[0].exit_epoch != spec.FAR_FUTURE_EPOCH

    assert len(state.pending_consolidations) == pre_pending_consolidations_len + 1
    pending_consolidation = state.pending_consolidations[pre_pending_consolidations_len]
    assert pending_consolidation.source_index == 1
    assert pending_consolidation.target_index == 2
    assert state.validators[1].exit_epoch != spec.FAR_FUTURE_EPOCH

    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len + 1
    pending_withdrawal = state.builder_pending_withdrawals[pre_pending_withdrawals_len]
    assert pending_withdrawal.amount == payment_amount
    assert pending_withdrawal.builder_index == builder_index
    assert pending_withdrawal.fee_recipient == parent_bid.fee_recipient

    assert state.builder_pending_payments[payment_index] == spec.BuilderPendingPayment()
    assert state.execution_payload_availability[parent_slot % spec.SLOTS_PER_HISTORICAL_ROOT] == 0b1
    assert state.latest_block_hash == parent_bid.block_hash


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_empty_parent(spec, state):
    parent_slot = state.slot
    child_slot = parent_slot + 1

    parent_bid = _setup_parent_payload_state(spec, state, parent_slot)
    state.slot = child_slot
    block = _build_block_for_parent_processing(
        spec, state, child_slot, parent_bid, parent_full=False
    )
    pre_state = state.copy()

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert state == pre_state


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_empty_parent_with_nonempty_requests_invalid(spec, state):
    parent_slot = state.slot
    child_slot = parent_slot + 1

    parent_bid = _setup_parent_payload_state(spec, state, parent_slot)
    state.slot = child_slot
    execution_requests = _build_execution_requests(
        spec,
        withdrawals=[spec.WithdrawalRequest(amount=spec.FULL_EXIT_REQUEST_AMOUNT)],
    )
    block = _build_block_for_parent_processing(
        spec,
        state,
        child_slot,
        parent_bid,
        parent_execution_requests=execution_requests,
        parent_full=False,
    )

    yield from run_parent_execution_payload_processing(spec, state, block, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_parent_execution_payload_with_deposit_requests(spec, state):
    parent_slot = state.slot
    child_slot = parent_slot + 1

    parent_bid = _setup_parent_payload_state(spec, state, parent_slot)
    state.slot = child_slot

    deposit_requests = [
        prepare_deposit_request(
            spec,
            len(state.validators),
            spec.MIN_DEPOSIT_AMOUNT,
            index=0,
            signed=True,
        ),
        prepare_deposit_request(
            spec,
            len(state.validators) + 1,
            spec.MIN_DEPOSIT_AMOUNT + spec.Gwei(1),
            index=1,
            signed=True,
        ),
    ]
    execution_requests = _build_execution_requests(spec, deposits=deposit_requests)
    parent_bid = state.latest_execution_payload_bid
    block = _build_block_for_parent_processing(
        spec,
        state,
        child_slot,
        parent_bid,
        parent_execution_requests=execution_requests,
    )

    pre_pending_deposits_len = len(state.pending_deposits)

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert len(state.pending_deposits) == pre_pending_deposits_len + len(deposit_requests)
    for i, deposit_request in enumerate(deposit_requests):
        pending_deposit = state.pending_deposits[pre_pending_deposits_len + i]
        assert pending_deposit.pubkey == deposit_request.pubkey
        assert pending_deposit.withdrawal_credentials == deposit_request.withdrawal_credentials
        assert pending_deposit.amount == deposit_request.amount
        assert pending_deposit.slot == child_slot


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_builder_payment(spec, state):
    parent_slot = state.slot
    child_slot = parent_slot + 1
    builder_index = 0
    payment_amount = spec.Gwei(7_000_000)

    parent_bid = _setup_parent_payload_state(
        spec, state, parent_slot, builder_index=builder_index, value=payment_amount
    )
    state.slot = child_slot
    block = _build_block_for_parent_processing(spec, state, child_slot, parent_bid)

    payment_index = spec.SLOTS_PER_EPOCH + parent_slot % spec.SLOTS_PER_EPOCH
    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len + 1
    withdrawal = state.builder_pending_withdrawals[pre_pending_withdrawals_len]
    assert withdrawal.amount == payment_amount
    assert withdrawal.builder_index == builder_index
    assert withdrawal.fee_recipient == parent_bid.fee_recipient
    assert state.builder_pending_payments[payment_index] == spec.BuilderPendingPayment()


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_cross_epoch(spec, state):
    parent_slot = spec.compute_start_slot_at_epoch(spec.Epoch(1)) - 1
    child_slot = parent_slot + 1
    builder_index = 0
    payment_amount = spec.Gwei(9_000_000)

    parent_bid = _setup_parent_payload_state(
        spec, state, parent_slot, builder_index=builder_index, value=payment_amount
    )
    _rotate_builder_pending_payments(spec, state)
    state.slot = child_slot
    block = _build_block_for_parent_processing(spec, state, child_slot, parent_bid)

    payment_index = parent_slot % spec.SLOTS_PER_EPOCH
    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len + 1
    withdrawal = state.builder_pending_withdrawals[pre_pending_withdrawals_len]
    assert withdrawal.amount == payment_amount
    assert withdrawal.builder_index == builder_index
    assert state.builder_pending_payments[payment_index] == spec.BuilderPendingPayment()


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_parent_execution_payload_builder_deposit_after_pending_validator(spec, state):
    parent_slot = state.slot
    child_slot = parent_slot + 1

    parent_bid = _setup_parent_payload_state(spec, state, parent_slot)
    state.slot = child_slot

    new_validator_index = len(state.validators)
    amount = spec.MIN_DEPOSIT_AMOUNT
    deposit_request_1 = prepare_deposit_request(
        spec,
        new_validator_index,
        amount,
        index=0,
        withdrawal_credentials=make_withdrawal_credentials(
            spec, spec.ETH1_ADDRESS_WITHDRAWAL_PREFIX, b"\xab"
        ),
        signed=True,
    )
    deposit_request_2 = prepare_deposit_request(
        spec,
        new_validator_index,
        amount,
        index=1,
        withdrawal_credentials=make_withdrawal_credentials(
            spec, spec.BUILDER_WITHDRAWAL_PREFIX, b"\x59"
        ),
        signed=True,
    )

    execution_requests = _build_execution_requests(
        spec,
        deposits=[deposit_request_1, deposit_request_2],
    )
    parent_bid = state.latest_execution_payload_bid
    block = _build_block_for_parent_processing(
        spec,
        state,
        child_slot,
        parent_bid,
        parent_execution_requests=execution_requests,
    )

    pre_pending_deposits_len = len(state.pending_deposits)
    pre_builder_count = len(state.builders)

    yield from run_parent_execution_payload_processing(spec, state, block)

    assert len(state.pending_deposits) == pre_pending_deposits_len + 2
    assert len(state.builders) == pre_builder_count
    first = state.pending_deposits[pre_pending_deposits_len]
    second = state.pending_deposits[pre_pending_deposits_len + 1]
    assert first.pubkey == deposit_request_1.pubkey
    assert first.withdrawal_credentials == deposit_request_1.withdrawal_credentials
    assert first.amount == deposit_request_1.amount
    assert first.slot == child_slot
    assert second.pubkey == deposit_request_2.pubkey
    assert second.withdrawal_credentials == deposit_request_2.withdrawal_credentials
    assert second.amount == deposit_request_2.amount
    assert second.slot == child_slot
