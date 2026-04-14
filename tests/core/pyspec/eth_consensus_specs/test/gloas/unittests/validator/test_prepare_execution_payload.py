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


def _setup_store_with_block(spec, state):
    """Set up genesis store, add one block, return (store, signed_block, block_root, test_steps)."""
    test_steps = []
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)
    block_root = signed_block.message.hash_tree_root()

    return store, signed_block, block_root, test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__full_parent(spec, state):
    """
    When the parent's execution payload has been seen (FULL),
    prepare_execution_payload should:
    - use parent_bid.block_hash as execution head
    - compute withdrawals from post-apply_parent_execution_payload state
    """
    test_steps = []
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)
    yield "anchor_state", state
    yield "anchor_block", store.blocks[spec.get_head(store).root]

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    # Add a block
    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)
    block_root = signed_block.message.hash_tree_root()

    # Deliver the execution payload envelope → head becomes FULL
    envelope = build_signed_execution_payload_envelope(spec, state, block_root, signed_block)
    yield from add_execution_payload(spec, store, envelope, test_steps, valid=True)

    head = spec.get_head(store)
    assert head.payload_status == spec.PAYLOAD_STATUS_FULL

    # Advance state to proposal slot
    proposal_state = state.copy()
    spec.process_slots(proposal_state, proposal_state.slot + 1)

    # Advance store time to proposal slot
    proposal_time = (
        store.genesis_time + proposal_state.slot * spec.config.SLOT_DURATION_MS // 1000
    )
    on_tick_and_append_step(spec, store, proposal_time, test_steps)

    # Call prepare_execution_payload with capture engine
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

    # FULL parent → execution head should be parent_bid.block_hash
    assert engine.head_block_hash == parent_bid.block_hash

    # Compute expected withdrawals: apply parent payload to a copy, then get_expected_withdrawals
    expected_state = proposal_state.copy()
    spec.apply_parent_execution_payload(
        expected_state, parent_bid, envelope.message.execution_requests
    )
    expected_withdrawals = spec.get_expected_withdrawals(expected_state).withdrawals
    assert engine.payload_attributes.withdrawals == expected_withdrawals

    yield "steps", test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__empty_parent(spec, state):
    """
    When the parent's execution payload has NOT been seen (EMPTY/PENDING),
    prepare_execution_payload should:
    - use parent_bid.parent_block_hash as execution head
    - use cached state.payload_expected_withdrawals
    """
    test_steps = []
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)
    yield "anchor_state", state
    yield "anchor_block", store.blocks[spec.get_head(store).root]

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    # Add a block but do NOT deliver envelope → head stays EMPTY
    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)

    head = spec.get_head(store)
    assert head.payload_status == spec.PAYLOAD_STATUS_EMPTY

    # Advance state to proposal slot
    proposal_state = state.copy()
    spec.process_slots(proposal_state, proposal_state.slot + 1)

    # Advance store time to proposal slot
    proposal_time = (
        store.genesis_time + proposal_state.slot * spec.config.SLOT_DURATION_MS // 1000
    )
    on_tick_and_append_step(spec, store, proposal_time, test_steps)

    # Call prepare_execution_payload with capture engine
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

    # EMPTY parent → execution head should be parent_bid.parent_block_hash
    assert engine.head_block_hash == parent_bid.parent_block_hash

    # EMPTY parent → withdrawals should be the cached stale list
    assert engine.payload_attributes.withdrawals == proposal_state.payload_expected_withdrawals

    yield "steps", test_steps


@with_gloas_and_later
@spec_state_test
def test_prepare_execution_payload__full_parent_does_not_mutate_state(spec, state):
    """
    When the parent is FULL, prepare_execution_payload copies state
    before applying parent payload. The original state must be unchanged.
    """
    test_steps = []
    store, _ = get_genesis_forkchoice_store_and_block(spec, state)
    yield "anchor_state", state
    yield "anchor_block", store.blocks[spec.get_head(store).root]

    current_time = state.slot * (spec.config.SLOT_DURATION_MS // 1000) + store.genesis_time
    on_tick_and_append_step(spec, store, current_time, test_steps)

    # Add a block and deliver envelope → FULL
    block = build_empty_block_for_next_slot(spec, state)
    signed_block = state_transition_and_sign_block(spec, state, block)
    yield from tick_and_add_block(spec, store, signed_block, test_steps)
    block_root = signed_block.message.hash_tree_root()

    envelope = build_signed_execution_payload_envelope(spec, state, block_root, signed_block)
    yield from add_execution_payload(spec, store, envelope, test_steps, valid=True)

    # Advance state to proposal slot
    proposal_state = state.copy()
    spec.process_slots(proposal_state, proposal_state.slot + 1)

    # Advance store time
    proposal_time = (
        store.genesis_time + proposal_state.slot * spec.config.SLOT_DURATION_MS // 1000
    )
    on_tick_and_append_step(spec, store, proposal_time, test_steps)

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
