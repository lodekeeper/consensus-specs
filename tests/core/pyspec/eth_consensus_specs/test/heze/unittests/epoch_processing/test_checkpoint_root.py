from eth_consensus_specs.test.context import spec_state_test, with_heze_and_later
from eth_consensus_specs.test.helpers.block import (
    build_empty_block,
    build_empty_block_for_next_slot,
    sign_block,
)
from eth_consensus_specs.test.helpers.state import next_slot, state_transition_and_sign_block


@with_heze_and_later
@spec_state_test
def test_checkpoint_root_anchors_to_previous_epoch_boundary(spec, state):
    # Build a block at every slot through the first slot of epoch 1, so that
    # both the last slot of epoch 0 and the first slot of epoch 1 hold blocks.
    epoch_1_start = spec.compute_start_slot_at_epoch(spec.GENESIS_EPOCH + 1)
    while state.slot < epoch_1_start + 1:
        block = build_empty_block_for_next_slot(spec, state)
        state_transition_and_sign_block(spec, state, block)

    epoch = spec.get_current_epoch(state)
    assert epoch == spec.GENESIS_EPOCH + 1
    start_slot = spec.compute_start_slot_at_epoch(epoch)

    boundary_root = spec.get_block_root_at_slot(state, spec.Slot(start_slot - 1))
    first_slot_root = spec.get_block_root_at_slot(state, start_slot)
    # The two candidate roots are distinct blocks
    assert boundary_root != first_slot_root

    # get_checkpoint_root anchors to the last block of the previous epoch, which
    # is what the modified FFG target votes for -- not the first block of the
    # epoch that get_block_root returns.
    assert spec.get_checkpoint_root(state, epoch) == boundary_root
    assert spec.get_block_root(state, epoch) == first_slot_root
    assert spec.get_checkpoint_root(state, epoch) != spec.get_block_root(state, epoch)


@with_heze_and_later
@spec_state_test
def test_checkpoint_root_empty_boundary_slot_matches_get_block_root(spec, state):
    # When the first slot of the epoch is empty, get_block_root already falls
    # back to the previous epoch's last block, so the two agree.
    next_slot(spec, state)
    block = build_empty_block_for_next_slot(spec, state)
    state_transition_and_sign_block(spec, state, block)
    # Advance across the epoch boundary without producing a block at slot 0
    for _ in range(spec.SLOTS_PER_EPOCH):
        next_slot(spec, state)

    epoch = spec.get_current_epoch(state)
    start_slot = spec.compute_start_slot_at_epoch(epoch)
    # The first slot of the epoch is empty
    assert state.block_roots[
        start_slot % spec.SLOTS_PER_HISTORICAL_ROOT
    ] == spec.get_block_root_at_slot(state, spec.Slot(start_slot - 1))
    assert spec.get_checkpoint_root(state, epoch) == spec.get_block_root(state, epoch)


@with_heze_and_later
@spec_state_test
def test_checkpoint_root_genesis_epoch(spec, state):
    # The genesis epoch has no previous epoch; the checkpoint anchors to the
    # genesis block.
    next_slot(spec, state)
    genesis_root = spec.get_block_root_at_slot(state, spec.GENESIS_SLOT)
    assert spec.get_checkpoint_root(state, spec.GENESIS_EPOCH) == genesis_root


@with_heze_and_later
@spec_state_test
def test_forkchoice_store_accepts_dialed_anchor_state(spec, state):
    # Build before the final slot of epoch 0, then skip the boundary slot. In
    # Heze, this earlier block is still the checkpoint block for epoch 1.
    anchor_epoch = spec.GENESIS_EPOCH + 1
    epoch_start = spec.compute_start_slot_at_epoch(anchor_epoch)
    anchor_slot = spec.Slot(epoch_start - 2)
    signed_anchor_block = None
    while state.slot < anchor_slot:
        block = build_empty_block_for_next_slot(spec, state)
        signed_anchor_block = state_transition_and_sign_block(spec, state, block)

    anchor_block = signed_anchor_block.message.copy()
    anchor_post_state = state.copy()
    anchor_state = state.copy()
    spec.process_slots(anchor_state, epoch_start)
    anchor_root = spec.hash_tree_root(anchor_block)
    checkpoint = spec.Checkpoint(epoch=anchor_epoch, root=anchor_root)

    store = spec.get_forkchoice_store(anchor_state, anchor_block)

    # The trusted anchor state is dialed to the start of the checkpoint epoch, so
    # it no longer matches the anchor block post-state when the boundary slot is
    # skipped.
    assert anchor_block.state_root == spec.hash_tree_root(anchor_post_state)
    assert anchor_block.state_root != spec.hash_tree_root(anchor_state)
    assert anchor_state.slot == epoch_start

    # Initialize fork choice at epoch 1 while anchoring the checkpoint root to
    # the most recent block before that epoch.
    assert store.justified_checkpoint == checkpoint
    assert store.finalized_checkpoint == checkpoint
    assert store.checkpoint_states[checkpoint].slot == epoch_start
    assert store.block_states[anchor_root].slot == epoch_start
    assert (
        store.time == anchor_state.genesis_time + epoch_start * spec.config.SLOT_DURATION_MS // 1000
    )

    # The first post-anchor block can be at the same slot as the dialed anchor
    # state. Heze fork choice must process it directly instead of calling the
    # state_transition() wrapper, which expects state.slot < block.slot.
    child_block = build_empty_block(spec, anchor_state, epoch_start)
    child_state = anchor_state.copy()
    spec.process_block(child_state, child_block)
    child_block.state_root = child_state.hash_tree_root()
    signed_child_block = sign_block(spec, anchor_state, child_block)
    child_root = spec.hash_tree_root(child_block)

    spec.on_block(store, signed_child_block)

    assert child_root in store.blocks
    assert store.block_states[child_root].hash_tree_root() == child_block.state_root
