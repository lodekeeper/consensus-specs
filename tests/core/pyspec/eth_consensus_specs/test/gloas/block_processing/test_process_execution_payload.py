from eth_consensus_specs.test.context import (
    always_bls,
    expect_assertion_error,
    spec_state_test,
    with_gloas_and_later,
)
from eth_consensus_specs.test.helpers.execution_payload import (
    build_empty_execution_payload,
)
from eth_consensus_specs.test.helpers.keys import builder_privkeys, privkeys


def run_execution_payload_processing(
    spec, state, signed_envelope, valid=True, execution_valid=True
):
    """
    Run ``process_execution_payload``, yielding:
    - pre-state ('pre')
    - signed_envelope ('signed_envelope')
    - execution details ('execution.yml')
    - post-state ('post').
    If ``valid == False``, run expecting ``AssertionError``
    """
    yield "pre", state
    yield "signed_envelope", signed_envelope
    yield "execution", {"execution_valid": execution_valid}

    called_new_payload = False
    pre_state_root = state.hash_tree_root()

    class TestEngine(spec.NoopExecutionEngine):
        def verify_and_notify_new_payload(self, new_payload_request) -> bool:
            nonlocal called_new_payload
            called_new_payload = True
            assert new_payload_request.execution_payload == signed_envelope.message.payload
            return execution_valid

    if not valid:
        expect_assertion_error(
            lambda: spec.process_execution_payload(
                state, signed_envelope, TestEngine(), verify=True
            )
        )
        yield "post", None
        return

    verified_requests = spec.process_execution_payload(state, signed_envelope, TestEngine(), verify=True)

    # Make sure we called the engine
    assert called_new_payload
    # Payload processing is verification-only under the deferred model
    assert state.hash_tree_root() == pre_state_root
    assert verified_requests == signed_envelope.message.execution_requests

    yield "post", state
    return verified_requests


def prepare_execution_payload_envelope(
    spec,
    state,
    builder_index=None,
    slot=None,
    beacon_block_root=None,
    execution_payload=None,
    execution_requests=None,
    valid_signature=True,
):
    """
    Helper to create a signed execution payload envelope with customizable parameters.
    Note: This should be called AFTER setting up the state with the committed bid.
    """
    if builder_index is None:
        builder_index = spec.BUILDER_INDEX_SELF_BUILD

    if slot is None:
        slot = state.slot

    if beacon_block_root is None:
        # Cache latest block header state root if not already set
        if state.latest_block_header.state_root == spec.Root():
            state.latest_block_header.state_root = state.hash_tree_root()
        beacon_block_root = state.latest_block_header.hash_tree_root()

    if execution_payload is None:
        execution_payload = build_empty_execution_payload(spec, state)

    if execution_requests is None:
        execution_requests = spec.ExecutionRequests(
            deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](),
            withdrawals=spec.List[
                spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD
            ](),
            consolidations=spec.List[
                spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
            ](),
        )

    envelope = spec.ExecutionPayloadEnvelope(
        payload=execution_payload,
        execution_requests=execution_requests,
        builder_index=builder_index,
        beacon_block_root=beacon_block_root,
        slot=slot,
    )

    if valid_signature:
        if envelope.builder_index == spec.BUILDER_INDEX_SELF_BUILD:
            privkey = privkeys[state.latest_block_header.proposer_index]
        else:
            privkey = builder_privkeys[envelope.builder_index]
        signature = spec.get_execution_payload_envelope_signature(
            state,
            envelope,
            privkey,
        )
    else:
        # Invalid signature
        signature = spec.BLSSignature()

    return spec.SignedExecutionPayloadEnvelope(
        message=envelope,
        signature=signature,
    )


def setup_state_with_payload_bid(
    spec, state, builder_index=None, value=None, prev_randao=None, blob_kzg_commitments=None
):
    """
    Helper to setup state with a committed execution payload bid.
    This simulates the state after process_execution_payload_bid has run.
    """
    if builder_index is None:
        builder_index = spec.BUILDER_INDEX_SELF_BUILD

    if value is None:
        value = spec.Gwei(0)

    if prev_randao is None:
        prev_randao = spec.get_randao_mix(state, spec.get_current_epoch(state))

    if blob_kzg_commitments is None:
        blob_kzg_commitments = spec.List[spec.KZGCommitment, spec.MAX_BLOB_COMMITMENTS_PER_BLOCK]()

    # Create and set the latest execution payload bid
    bid = spec.ExecutionPayloadBid(
        parent_block_hash=state.latest_block_hash,
        parent_block_root=state.latest_block_header.hash_tree_root(),
        block_hash=spec.Hash32(),
        prev_randao=prev_randao,
        fee_recipient=spec.ExecutionAddress(),
        gas_limit=spec.uint64(60000000),
        builder_index=builder_index,
        slot=state.slot,
        value=value,
        execution_payment=spec.Gwei(0),
        blob_kzg_commitments=blob_kzg_commitments,
        execution_requests_root=spec.hash_tree_root(spec.ExecutionRequests()),
    )
    state.latest_execution_payload_bid = bid

    # Setup withdrawals root
    state.payload_expected_withdrawals = spec.List[
        spec.Withdrawal, spec.MAX_WITHDRAWALS_PER_PAYLOAD
    ]()

    # Add pending payment if value > 0
    if value > 0:
        pending_payment = spec.BuilderPendingPayment(
            weight=0,
            withdrawal=spec.BuilderPendingWithdrawal(
                fee_recipient=bid.fee_recipient,
                amount=value,
                builder_index=builder_index,
            ),
        )
        state.builder_pending_payments[spec.SLOTS_PER_EPOCH + state.slot % spec.SLOTS_PER_EPOCH] = (
            pending_payment
        )


#
# Valid cases
#


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_valid(spec, state):
    """
    Test valid execution payload verification with a non-zero builder payment.
    """
    builder_index = 0
    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(50000000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    pre_payment = state.builder_pending_payments[
        spec.SLOTS_PER_EPOCH + state.slot % spec.SLOTS_PER_EPOCH
    ]
    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)
    pre_latest_block_hash = state.latest_block_hash
    pre_availability = state.execution_payload_availability[state.slot % spec.SLOTS_PER_HISTORICAL_ROOT]

    verified_requests = yield from run_execution_payload_processing(spec, state, signed_envelope)

    assert verified_requests == spec.ExecutionRequests()
    assert state.latest_block_hash == pre_latest_block_hash
    assert (
        state.execution_payload_availability[state.slot % spec.SLOTS_PER_HISTORICAL_ROOT]
        == pre_availability
    )
    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len
    assert (
        state.builder_pending_payments[
            spec.SLOTS_PER_EPOCH + state.slot % spec.SLOTS_PER_EPOCH
        ]
        == pre_payment
    )


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_self_build_zero_value(spec, state):
    """
    Test valid self-build payload verification.
    """
    setup_state_with_payload_bid(spec, state, spec.BUILDER_INDEX_SELF_BUILD, spec.Gwei(0))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=spec.BUILDER_INDEX_SELF_BUILD,
        execution_payload=execution_payload,
    )

    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)
    pre_latest_block_hash = state.latest_block_hash

    verified_requests = yield from run_execution_payload_processing(spec, state, signed_envelope)

    assert verified_requests == spec.ExecutionRequests()
    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len
    assert state.latest_block_hash == pre_latest_block_hash


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_large_payment_churn_impact(spec, state):
    """
    Test that large pending builder payments remain deferred until the child block.
    """
    builder_index = 0
    large_payment_amount = spec.Gwei(500000000000)
    setup_state_with_payload_bid(spec, state, builder_index, large_payment_amount)

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
    )

    pre_payment = state.builder_pending_payments[
        spec.SLOTS_PER_EPOCH + state.slot % spec.SLOTS_PER_EPOCH
    ]
    pre_pending_withdrawals_len = len(state.builder_pending_withdrawals)

    yield from run_execution_payload_processing(spec, state, signed_envelope)

    assert len(state.builder_pending_withdrawals) == pre_pending_withdrawals_len
    assert (
        state.builder_pending_payments[
            spec.SLOTS_PER_EPOCH + state.slot % spec.SLOTS_PER_EPOCH
        ]
        == pre_payment
    )


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_with_blob_commitments(spec, state):
    """
    Test execution payload verification with blob KZG commitments.
    """
    builder_index = 0
    setup_state_with_payload_bid(
        spec,
        state,
        builder_index,
        spec.Gwei(3000000),
        blob_kzg_commitments=[spec.KZGCommitment(b"\x42" * 48), spec.KZGCommitment(b"\x43" * 48)],
    )

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
    )

    verified_requests = yield from run_execution_payload_processing(spec, state, signed_envelope)
    assert verified_requests == spec.ExecutionRequests()


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_with_execution_requests(spec, state):
    """
    Test execution payload verification returns execution requests without mutating CL state.
    """
    builder_index = 0
    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(4000000))

    execution_requests = spec.ExecutionRequests(
        deposits=spec.List[spec.DepositRequest, spec.MAX_DEPOSIT_REQUESTS_PER_PAYLOAD](
            [
                spec.DepositRequest(
                    pubkey=spec.BLSPubkey(b"\x01" * 48),
                    withdrawal_credentials=spec.Bytes32(b"\x02" * 32),
                    amount=spec.Gwei(32000000000),
                    signature=spec.BLSSignature(b"\x03" * 96),
                    index=spec.uint64(0),
                )
            ]
        ),
        withdrawals=spec.List[spec.WithdrawalRequest, spec.MAX_WITHDRAWAL_REQUESTS_PER_PAYLOAD](
            [
                spec.WithdrawalRequest(
                    source_address=spec.ExecutionAddress(b"\x04" * 20),
                    validator_pubkey=spec.BLSPubkey(b"\x05" * 48),
                    amount=spec.Gwei(16000000000),
                )
            ]
        ),
        consolidations=spec.List[
            spec.ConsolidationRequest, spec.MAX_CONSOLIDATION_REQUESTS_PER_PAYLOAD
        ](
            [
                spec.ConsolidationRequest(
                    source_address=spec.ExecutionAddress(b"\x06" * 20),
                    source_pubkey=spec.BLSPubkey(b"\x07" * 48),
                    target_pubkey=spec.BLSPubkey(b"\x08" * 48),
                )
            ]
        ),
    )

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
        execution_requests=execution_requests,
    )

    pre_pending_deposits_len = len(state.pending_deposits)
    pre_builder_count = len(state.builders)

    verified_requests = yield from run_execution_payload_processing(spec, state, signed_envelope)

    assert verified_requests == execution_requests
    assert len(state.pending_deposits) == pre_pending_deposits_len
    assert len(state.builders) == pre_builder_count


#
# Invalid signature tests
#


@with_gloas_and_later
@spec_state_test
def test_process_execution_payload_invalid_signature(spec, state):
    """
    Test invalid signature fails with separate builder and non-zero payment
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(2000000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
        valid_signature=False,
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_beacon_block_root(spec, state):
    """
    Test wrong beacon block root fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(1500000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    wrong_beacon_block_root = spec.Root(b"\x42" * 32)
    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
        beacon_block_root=wrong_beacon_block_root,
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_slot(spec, state):
    """
    Test wrong slot fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(2500000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=builder_index,
        execution_payload=execution_payload,
        slot=state.slot + 1,  # Wrong slot
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_builder_index(spec, state):
    """
    Test wrong builder index fails with separate builders
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(3500000))

    # Use different builder index in envelope
    other_builder_index = 1

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec,
        state,
        builder_index=other_builder_index,  # Wrong builder
        execution_payload=execution_payload,
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_missing_expected_withdrawal(spec, state):
    """
    Verify payload rejected when it omits a withdrawal expected by the state.
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(2600000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    withdrawal = spec.Withdrawal(
        index=0,
        validator_index=0,
        address=b"\x22" * 20,
        amount=spec.Gwei(1),
    )
    state.payload_expected_withdrawals = spec.List[
        spec.Withdrawal, spec.MAX_WITHDRAWALS_PER_PAYLOAD
    ]([withdrawal])
    execution_payload.withdrawals = spec.List[spec.Withdrawal, spec.MAX_WITHDRAWALS_PER_PAYLOAD]()

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_gas_limit(spec, state):
    """
    Test wrong gas limit fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(1800000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = (
        state.latest_execution_payload_bid.gas_limit + 1
    )  # Wrong gas limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_block_hash(spec, state):
    """
    Test wrong block hash fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(2200000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = spec.Hash32(b"\x42" * 32)  # Wrong block hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_parent_hash(spec, state):
    """
    Test wrong parent hash fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(1600000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = spec.Hash32(b"\x42" * 32)  # Wrong parent hash

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_prev_randao(spec, state):
    """
    Test wrong prev_randao fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(2100000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    execution_payload.prev_randao = spec.Bytes32(b"\x42" * 32)  # Wrong prev_randao

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_bid_prev_randao_mismatch(spec, state):
    """
    Test that committed_bid.prev_randao must equal payload.prev_randao
    """
    builder_index = 0

    # Setup bid with one prev_randao value
    bid_prev_randao = spec.Bytes32(b"\x11" * 32)
    setup_state_with_payload_bid(
        spec, state, builder_index, spec.Gwei(2300000), prev_randao=bid_prev_randao
    )

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    # Set payload with a different prev_randao value
    execution_payload.prev_randao = spec.Bytes32(b"\x22" * 32)

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_wrong_timestamp(spec, state):
    """
    Test wrong timestamp fails with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(1900000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash
    execution_payload.timestamp = execution_payload.timestamp + 1  # Wrong timestamp

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(spec, state, signed_envelope, valid=False)


@with_gloas_and_later
@spec_state_test
@always_bls
def test_process_execution_payload_execution_engine_invalid(spec, state):
    """
    Test execution engine returns invalid with separate builder
    """
    builder_index = 0

    setup_state_with_payload_bid(spec, state, builder_index, spec.Gwei(3200000))

    execution_payload = build_empty_execution_payload(spec, state)
    execution_payload.block_hash = state.latest_execution_payload_bid.block_hash
    execution_payload.gas_limit = state.latest_execution_payload_bid.gas_limit
    execution_payload.parent_hash = state.latest_block_hash

    signed_envelope = prepare_execution_payload_envelope(
        spec, state, builder_index=builder_index, execution_payload=execution_payload
    )

    yield from run_execution_payload_processing(
        spec, state, signed_envelope, valid=False, execution_valid=False
    )
