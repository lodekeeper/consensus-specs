from eth_consensus_specs.test.context import (
    always_bls,
    expect_assertion_error,
    spec_state_test,
    with_gloas_and_later,
)
from eth_consensus_specs.test.helpers.deposits import (
    make_withdrawal_credentials,
    prepare_deposit_request,
)


def _set_parent_header(spec, state, parent_slot):
    header = state.latest_block_header
    state.latest_block_header = spec.BeaconBlockHeader(
        slot=parent_slot,
        proposer_index=header.proposer_index,
        parent_root=header.parent_root,
        state_root=header.state_root,
        body_root=header.body_root,
    )


def _build_execution_payload_bid(
    spec,
    state,
    parent_block_hash,
    block_hash,
    slot,
    execution_requests=None,
):
    if execution_requests is None:
        execution_requests = spec.ExecutionRequests()

    return spec.ExecutionPayloadBid(
        parent_block_hash=parent_block_hash,
        parent_block_root=state.latest_block_header.hash_tree_root(),
        block_hash=block_hash,
        prev_randao=spec.Bytes32(),
        fee_recipient=spec.ExecutionAddress(),
        gas_limit=spec.uint64(0),
        builder_index=spec.BUILDER_INDEX_SELF_BUILD,
        slot=slot,
        value=spec.Gwei(0),
        execution_payment=spec.Gwei(0),
        blob_kzg_commitments=spec.List[
            spec.KZGCommitment, spec.MAX_BLOB_COMMITMENTS_PER_BLOCK
        ](),
        execution_requests_root=spec.hash_tree_root(execution_requests),
    )


def _set_parent_payload_state(
    spec,
    state,
    *,
    parent_slot,
    previous_full_hash,
    parent_payload_hash,
    execution_requests=None,
    payment_amount=0,
):
    _set_parent_header(spec, state, parent_slot)
    state.latest_execution_payload_bid = _build_execution_payload_bid(
        spec,
        state,
        parent_block_hash=previous_full_hash,
        block_hash=parent_payload_hash,
        slot=parent_slot,
        execution_requests=execution_requests,
    )
    state.latest_block_hash = previous_full_hash
    state.execution_payload_availability[parent_slot % spec.SLOTS_PER_HISTORICAL_ROOT] = 0b0

    if payment_amount > 0:
        if len(state.builders) == 0:
            state.builders.append(
                spec.Builder(
                    pubkey=spec.BLSPubkey(b"\x11" * 48),
                    version=spec.uint8(0),
                    execution_address=spec.ExecutionAddress(b"\x22" * 20),
                    balance=spec.Gwei(spec.MIN_DEPOSIT_AMOUNT + payment_amount),
                    deposit_epoch=spec.compute_epoch_at_slot(parent_slot),
                    withdrawable_epoch=spec.FAR_FUTURE_EPOCH,
                )
            )

        payment_index = spec.SLOTS_PER_EPOCH + parent_slot % spec.SLOTS_PER_EPOCH
        if spec.compute_epoch_at_slot(parent_slot) != spec.get_current_epoch(state):
            payment_index = parent_slot % spec.SLOTS_PER_EPOCH
        state.builder_pending_payments[payment_index] = spec.BuilderPendingPayment(
            weight=spec.Gwei(0),
            withdrawal=spec.BuilderPendingWithdrawal(
                fee_recipient=spec.ExecutionAddress(b"\x33" * 20),
                amount=spec.Gwei(payment_amount),
                builder_index=spec.BuilderIndex(0),
            ),
        )


def _build_block(spec, state, *, parent_block_hash, parent_execution_requests=None):
    if parent_execution_requests is None:
        parent_execution_requests = spec.ExecutionRequests()

    block = spec.BeaconBlock()
    block.slot = state.slot
    block.body.signed_execution_payload_bid = spec.SignedExecutionPayloadBid(
        message=_build_execution_payload_bid(
            spec,
            state,
            parent_block_hash=parent_block_hash,
            block_hash=spec.Hash32(b"\x44" * 32),
            slot=state.slot,
            execution_requests=parent_execution_requests,
        ),
        signature=spec.G2_POINT_AT_INFINITY,
    )
    block.body.parent_execution_requests = parent_execution_requests
    return block


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_full_parent_with_empty_requests(spec, state):
    """
    A full parent with empty execution requests still updates deferred parent effects.
    """
    state.slot = spec.Slot(1)
    previous_full_hash = spec.Hash32(b"\xaa" * 32)
    parent_payload_hash = spec.Hash32(b"\xbb" * 32)
    payment_amount = spec.Gwei(123456789)
    _set_parent_payload_state(
        spec,
        state,
        parent_slot=spec.Slot(0),
        previous_full_hash=previous_full_hash,
        parent_payload_hash=parent_payload_hash,
        payment_amount=payment_amount,
    )

    block = _build_block(
        spec,
        state,
        parent_block_hash=parent_payload_hash,
        parent_execution_requests=spec.ExecutionRequests(),
    )

    payment_index = spec.SLOTS_PER_EPOCH
    spec.process_parent_execution_payload(state, block)

    assert state.latest_block_hash == parent_payload_hash
    assert state.execution_payload_availability[0] == 0b1
    assert len(state.builder_pending_withdrawals) == 1
    assert state.builder_pending_withdrawals[0].amount == payment_amount
    assert state.builder_pending_withdrawals[0].builder_index == spec.BuilderIndex(0)
    assert state.builder_pending_payments[payment_index] == spec.BuilderPendingPayment()


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_parent_execution_payload_routes_builder_deposit_after_pending_validator(spec, state):
    """
    Deferred execution request processing preserves in-envelope pending-validator ordering.
    """
    state.slot = spec.Slot(1)
    previous_full_hash = spec.Hash32(b"\xaa" * 32)
    parent_payload_hash = spec.Hash32(b"\xbb" * 32)
    _set_parent_payload_state(
        spec,
        state,
        parent_slot=spec.Slot(0),
        previous_full_hash=previous_full_hash,
        parent_payload_hash=parent_payload_hash,
    )

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
    requests = spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](
            [deposit_request_1, deposit_request_2]
        ),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](),
    )
    state.latest_execution_payload_bid = _build_execution_payload_bid(
        spec,
        state,
        parent_block_hash=previous_full_hash,
        block_hash=parent_payload_hash,
        slot=spec.Slot(0),
        execution_requests=requests,
    )
    block = _build_block(
        spec,
        state,
        parent_block_hash=parent_payload_hash,
        parent_execution_requests=requests,
    )

    pre_pending_deposits_len = len(state.pending_deposits)
    pre_builder_count = len(state.builders)

    spec.process_parent_execution_payload(state, block)

    assert state.latest_block_hash == parent_payload_hash
    assert len(state.pending_deposits) == pre_pending_deposits_len + 2
    assert len(state.builders) == pre_builder_count
    first = state.pending_deposits[pre_pending_deposits_len]
    second = state.pending_deposits[pre_pending_deposits_len + 1]
    assert first.pubkey == deposit_request_1.pubkey
    assert first.withdrawal_credentials == deposit_request_1.withdrawal_credentials
    assert second.pubkey == deposit_request_2.pubkey
    assert second.withdrawal_credentials == deposit_request_2.withdrawal_credentials


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_parent_execution_payload_empty_parent_rejects_requests(spec, state):
    """
    An empty parent must not carry deferred execution requests in the child block.
    """
    state.slot = spec.Slot(1)
    previous_full_hash = spec.Hash32(b"\xaa" * 32)
    parent_payload_hash = spec.Hash32(b"\xbb" * 32)
    _set_parent_payload_state(
        spec,
        state,
        parent_slot=spec.Slot(0),
        previous_full_hash=previous_full_hash,
        parent_payload_hash=parent_payload_hash,
    )

    request = prepare_deposit_request(
        spec,
        len(state.validators),
        spec.MIN_DEPOSIT_AMOUNT,
        index=0,
        withdrawal_credentials=make_withdrawal_credentials(
            spec, spec.ETH1_ADDRESS_WITHDRAWAL_PREFIX, b"\xab"
        ),
        signed=True,
    )
    block = _build_block(
        spec,
        state,
        parent_block_hash=previous_full_hash,
        parent_execution_requests=spec.ExecutionRequests(
            deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD]([request]),
            withdrawals=spec.List[
                spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD
            ](),
            consolidations=spec.List[
                spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
            ](),
        ),
    )

    expect_assertion_error(lambda: spec.process_parent_execution_payload(state, block))


@with_gloas_and_later
@spec_state_test
def test_process_parent_execution_payload_previous_epoch_payment_index(spec, state):
    """
    Deferred parent processing clears the previous-epoch payment slot across an epoch boundary.
    """
    parent_slot = spec.Slot(spec.SLOTS_PER_EPOCH - 1)
    state.slot = spec.Slot(spec.SLOTS_PER_EPOCH)
    previous_full_hash = spec.Hash32(b"\xaa" * 32)
    parent_payload_hash = spec.Hash32(b"\xbb" * 32)
    payment_amount = spec.Gwei(987654321)
    _set_parent_payload_state(
        spec,
        state,
        parent_slot=parent_slot,
        previous_full_hash=previous_full_hash,
        parent_payload_hash=parent_payload_hash,
        payment_amount=payment_amount,
    )

    block = _build_block(spec, state, parent_block_hash=parent_payload_hash)

    payment_index = parent_slot % spec.SLOTS_PER_EPOCH
    future_index = spec.SLOTS_PER_EPOCH + parent_slot % spec.SLOTS_PER_EPOCH
    pre_future_payment = state.builder_pending_payments[future_index]

    spec.process_parent_execution_payload(state, block)

    assert state.latest_block_hash == parent_payload_hash
    assert len(state.builder_pending_withdrawals) == 1
    assert state.builder_pending_withdrawals[0].amount == payment_amount
    assert state.builder_pending_payments[payment_index] == spec.BuilderPendingPayment()
    assert state.builder_pending_payments[future_index] == pre_future_payment
