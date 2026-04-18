from eth_consensus_specs.test.context import (
    spec_state_test,
    with_gloas_and_later,
)
from eth_consensus_specs.test.helpers.block import (
    build_empty_block_for_next_slot,
)
from eth_consensus_specs.test.helpers.execution_payload import (
    build_signed_execution_payload_envelope,
)
from eth_consensus_specs.test.helpers.fork_choice import (
    add_execution_payload,
    get_genesis_forkchoice_store_and_block,
    on_tick_and_append_step,
    tick_and_add_block,
)
from eth_consensus_specs.test.helpers.state import (
    state_transition_and_sign_block,
)


SAMPLE_PAYLOAD_ID = b"\x12" * 8


class CaptureEngine:
    """Mock execution engine that captures the arguments to notify_forkchoice_updated."""

    def __init__(self):
        self.head_block_hash = None
        self.payload_attributes = None

    def notify_forkchoice_updated(self, head_block_hash, safe_block_hash, finalized_block_hash,
                                  payload_attributes):
        self.head_block_hash = head_block_hash
        self.payload_attributes = payload_attributes
        return SAMPLE_PAYLOAD_ID


def _setup_full_parent(spec, state, test_steps):
    """Add a block and deliver its envelope so is_payload_verified returns True."""
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)
    yield "anchor_state", state
    yield "anchor_block", store.blocks[spec.get_head(store).root]

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)
    block_root = signed_block.message.hash_tree_root()

    envelope = build_signed_execution_payload_envelope(spec, state, block_root, signed_block)
    yield from add_execution_payload(spec, store, envelope, test_steps, valid=True)

    # Verify precondition: payload is verified
    assert spec.is_payload_verified(store, block_root)

    return store, signed_block, block_root, envelope


def _advance_to_proposal_slot(spec, state, store, test_steps):
    """Advance state and store time to the next proposal slot."""
    proposal_state = state.copy()
    spec.process_slots(proposal_state, proposal_state.slot + 1)

    proposal_time = (
        store.genesis_time + proposal_state.slot * spec.config.SLOT_DURATION_MS // 1000
    )
    on_tick_and_append_step(spec, store, proposal_time, test_steps)

    return proposal_state


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__extend_payload(spec, state):
    """
    When the parent's payload is verified (is_payload_verified) and
    should_extend_payload returns True, prepare_execution_payload should:
    - use parent_bid.block_hash as execution head
    - compute withdrawals from the post-apply_parent_execution_payload state
    """
    test_steps = []
    store, signed_block, block_root, envelope = yield from _setup_full_parent(
        spec, state, test_steps
    )

    # Verify precondition: should_extend_payload returns True
    assert spec.should_extend_payload(store, block_root)

    proposal_state = _advance_to_proposal_slot(spec, state, store, test_steps)

    engine = CaptureEngine()
    parent_bid = proposal_state.latest_execution_payload_bid
    payload_id = spec.prepare_execution_payload(
        store=store,
        state=proposal_state,
        safe_block_hash=spec.Hash32(),
        finalized_block_hash=spec.Hash32(),
        suggested_fee_recipient=spec.ExecutionAddress(),
        execution_engine=engine,
    )

    assert payload_id == SAMPLE_PAYLOAD_ID

    # Extending payload → execution head is parent_bid.block_hash
    assert engine.head_block_hash == parent_bid.block_hash

    # Withdrawals are computed from post-apply state
    expected_state = proposal_state.copy()
    spec.apply_parent_execution_payload(
        expected_state, parent_bid, envelope.message.execution_requests
    )
    expected_withdrawals = spec.get_expected_withdrawals(expected_state).withdrawals
    assert engine.payload_attributes.withdrawals == expected_withdrawals

    yield "steps", test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__no_payload_verified(spec, state):
    """
    When the parent's payload has NOT been delivered (is_payload_verified
    returns False), prepare_execution_payload should:
    - use parent_bid.parent_block_hash as execution head
    - use cached state.payload_expected_withdrawals
    """
    test_steps = []
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)
    yield "anchor_state", state
    yield "anchor_block", store.blocks[spec.get_head(store).root]

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    # Add a block but do not deliver envelope
    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)
    block_root = signed_block.message.hash_tree_root()

    # Verify precondition: payload is not verified
    assert not spec.is_payload_verified(store, block_root)

    # For heze and later: is_payload_inclusion_list_satisfied asserts the
    # root has been tracked. Normally record_payload_inclusion_list_satisfaction
    # runs from on_execution_payload_envelope, but here no envelope is delivered.
    if hasattr(store, "payload_inclusion_list_satisfaction"):
        store.payload_inclusion_list_satisfaction[block_root] = False

    proposal_state = _advance_to_proposal_slot(spec, state, store, test_steps)

    engine = CaptureEngine()
    parent_bid = proposal_state.latest_execution_payload_bid
    payload_id = spec.prepare_execution_payload(
        store=store,
        state=proposal_state,
        safe_block_hash=spec.Hash32(),
        finalized_block_hash=spec.Hash32(),
        suggested_fee_recipient=spec.ExecutionAddress(),
        execution_engine=engine,
    )

    assert payload_id == SAMPLE_PAYLOAD_ID

    # No payload verified → execution head is parent_bid.parent_block_hash
    assert engine.head_block_hash == parent_bid.parent_block_hash

    # No payload verified → withdrawals are the cached list
    assert engine.payload_attributes.withdrawals == proposal_state.payload_expected_withdrawals

    yield "steps", test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__extend_payload_does_not_mutate_state(spec, state):
    """
    When extending the parent's payload, prepare_execution_payload must
    copy the state before applying apply_parent_execution_payload.
    The original state passed in must remain unchanged.
    """
    test_steps = []
    store, signed_block, block_root, envelope = yield from _setup_full_parent(
        spec, state, test_steps
    )

    proposal_state = _advance_to_proposal_slot(spec, state, store, test_steps)

    # Snapshot state root before calling prepare_execution_payload
    state_root_before = proposal_state.hash_tree_root()

    engine = CaptureEngine()
    spec.prepare_execution_payload(
        store=store,
        state=proposal_state,
        safe_block_hash=spec.Hash32(),
        finalized_block_hash=spec.Hash32(),
        suggested_fee_recipient=spec.ExecutionAddress(),
        execution_engine=engine,
    )

    # State must not be mutated
    assert proposal_state.hash_tree_root() == state_root_before

    yield "steps", test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__payload_attributes(spec, state):
    """
    Verify payload attributes are correctly derived from the state:
    - timestamp from compute_time_at_slot
    - prev_randao from get_randao_mix
    - parent_beacon_block_root from state.latest_block_header
    - slot_number from state.slot (EIP-7843)
    """
    test_steps = []
    store, signed_block, block_root, envelope = yield from _setup_full_parent(
        spec, state, test_steps
    )

    proposal_state = _advance_to_proposal_slot(spec, state, store, test_steps)

    engine = CaptureEngine()
    spec.prepare_execution_payload(
        store=store,
        state=proposal_state,
        safe_block_hash=spec.Hash32(),
        finalized_block_hash=spec.Hash32(),
        suggested_fee_recipient=spec.ExecutionAddress(),
        execution_engine=engine,
    )

    attrs = engine.payload_attributes
    assert attrs.timestamp == spec.compute_time_at_slot(proposal_state, proposal_state.slot)
    assert attrs.prev_randao == spec.get_randao_mix(
        proposal_state, spec.get_current_epoch(proposal_state)
    )
    assert attrs.parent_beacon_block_root == proposal_state.latest_block_header.hash_tree_root()
    assert attrs.slot_number == proposal_state.slot

    yield "steps", test_steps
