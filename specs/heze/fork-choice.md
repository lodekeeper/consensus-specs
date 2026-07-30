# Heze -- Fork Choice

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Configuration](#configuration)
  - [Time parameters](#time-parameters)
- [Protocols](#protocols)
  - [`ExecutionEngine`](#executionengine)
    - [New `is_inclusion_list_satisfied`](#new-is_inclusion_list_satisfied)
    - [Modified `notify_forkchoice_updated`](#modified-notify_forkchoice_updated)
- [Helpers](#helpers)
  - [Modified `PayloadAttributes`](#modified-payloadattributes)
  - [Modified `Store`](#modified-store)
  - [New `get_checkpoint_slot`](#new-get_checkpoint_slot)
  - [Modified `get_forkchoice_store`](#modified-get_forkchoice_store)
  - [New `record_payload_inclusion_list_satisfaction`](#new-record_payload_inclusion_list_satisfaction)
  - [New `is_payload_inclusion_list_satisfied`](#new-is_payload_inclusion_list_satisfied)
  - [Modified `should_extend_payload`](#modified-should_extend_payload)
  - [New `get_inclusion_list_due_ms`](#new-get_inclusion_list_due_ms)
  - [Modified `get_checkpoint_block`](#modified-get_checkpoint_block)
- [Handlers](#handlers)
  - [New `on_inclusion_list`](#new-on_inclusion_list)
  - [Modified `on_block`](#modified-on_block)
  - [Modified `on_execution_payload_envelope`](#modified-on_execution_payload_envelope)

<!-- mdformat-toc end -->

## Introduction

This is the modification of the fork choice accompanying the Heze upgrade.

## Configuration

### Time parameters

| Name                     | Value          |     Unit     |          Duration          |
| ------------------------ | -------------- | :----------: | :------------------------: |
| `INCLUSION_LIST_DUE_BPS` | `uint64(6667)` | basis points | ~67% of `SLOT_DURATION_MS` |

## Protocols

### `ExecutionEngine`

*Note*: The `is_inclusion_list_satisfied` function is added to the
`ExecutionEngine` protocol to instantiate the inclusion list constraints
validation.

The body of this function is implementation dependent. The Engine API may be
used to implement it with an external execution engine.

#### New `is_inclusion_list_satisfied`

```python
def is_inclusion_list_satisfied(
    self: ExecutionEngine,
    execution_payload: ExecutionPayload,
    inclusion_list_transactions: Sequence[Transaction],
) -> bool:
    """
    Return ``True`` if and only if ``execution_payload`` satisfies the inclusion
    list constraints with respect to ``inclusion_list_transactions``.
    """
```

#### Modified `notify_forkchoice_updated`

*Note*: The only change made is to the `PayloadAttributes` container through the
addition of `inclusion_list_transactions`. Otherwise,
`notify_forkchoice_updated` inherits all prior functionality.

*Note*: If the `inclusion_list_transactions` field of `payload_attributes` is
not empty, the payload build process MUST produce an execution payload that
satisfies the inclusion list constraints with respect to
`inclusion_list_transactions`.

```python
def notify_forkchoice_updated(
    self: ExecutionEngine,
    head_block_hash: Hash32,
    safe_block_hash: Hash32,
    finalized_block_hash: Hash32,
    payload_attributes: Optional[PayloadAttributes],
) -> Optional[PayloadId]: ...
```

## Helpers

### Modified `PayloadAttributes`

`PayloadAttributes` is extended with the `inclusion_list_transactions` field.

```python
@dataclass
class PayloadAttributes:
    timestamp: uint64
    prev_randao: Bytes32
    suggested_fee_recipient: ExecutionAddress
    withdrawals: Sequence[Withdrawal]
    parent_beacon_block_root: Root
    slot_number: uint64
    target_gas_limit: uint64
    # [New in Heze:EIP7805]
    inclusion_list_transactions: Sequence[Transaction]
```

### Modified `Store`

*Note*: `Store` is modified to track whether the execution payloads satisfy the
inclusion list constraints.

```python
@dataclass
class Store:
    time: uint64
    genesis_time: uint64
    justified_checkpoint: Checkpoint
    finalized_checkpoint: Checkpoint
    unrealized_justified_checkpoint: Checkpoint
    unrealized_finalized_checkpoint: Checkpoint
    proposer_boost_root: Root
    equivocating_indices: Set[ValidatorIndex]
    blocks: Dict[Root, BeaconBlock] = field(default_factory=dict)
    block_states: Dict[Root, BeaconState] = field(default_factory=dict)
    block_timeliness: Dict[Root, list[boolean]] = field(default_factory=dict)
    checkpoint_states: Dict[Checkpoint, BeaconState] = field(default_factory=dict)
    latest_messages: Dict[ValidatorIndex, LatestMessage] = field(default_factory=dict)
    unrealized_justifications: Dict[Root, Checkpoint] = field(default_factory=dict)
    payloads: Dict[Root, ExecutionPayloadEnvelope] = field(default_factory=dict)
    payload_timeliness_vote: Dict[Root, list[Optional[boolean]]] = field(default_factory=dict)
    payload_data_availability_vote: Dict[Root, list[Optional[boolean]]] = field(
        default_factory=dict
    )
    # [New in Heze:EIP7805]
    payload_inclusion_list_satisfaction: Dict[Root, boolean] = field(default_factory=dict)
```

### New `get_checkpoint_slot`

*Note*: `get_checkpoint_slot` returns the slot anchoring the checkpoint for
`epoch`. Unlike prior forks, which anchor checkpoints to the first slot of
`epoch`, Heze anchors checkpoints to the last slot of the previous epoch. For
the genesis epoch, which has no previous epoch, the checkpoint slot is the
genesis slot.

```python
def get_checkpoint_slot(epoch: Epoch) -> Slot:
    """
    Return the slot anchoring the checkpoint for ``epoch``.
    """
    if epoch == GENESIS_EPOCH:
        return compute_start_slot_at_epoch(GENESIS_EPOCH)
    return Slot(compute_start_slot_at_epoch(epoch) - 1)
```

### Modified `get_forkchoice_store`

*Note*: `get_forkchoice_store` is modified so that the trusted anchor block is
the Heze checkpoint block for `anchor_epoch` -- the most recent block at or
before the last slot of the previous epoch. `anchor_state` is the trusted state
for `anchor_epoch`, with slots processed through the start of the epoch. This
means `anchor_block.state_root` does not match `hash_tree_root(anchor_state)`.

```python
def get_forkchoice_store(anchor_state: BeaconState, anchor_block: BeaconBlock) -> Store:
    anchor_root = hash_tree_root(anchor_block)
    # [Modified in Heze:EIPXXXX]
    anchor_epoch = get_current_epoch(anchor_state)
    assert anchor_state.slot == compute_start_slot_at_epoch(anchor_epoch)
    assert anchor_block.slot <= get_checkpoint_slot(anchor_epoch)
    if anchor_epoch == GENESIS_EPOCH:
        assert anchor_block.state_root == hash_tree_root(anchor_state)
    else:
        assert hash_tree_root(anchor_state.latest_block_header) == anchor_root
    justified_checkpoint = Checkpoint(epoch=anchor_epoch, root=anchor_root)
    finalized_checkpoint = Checkpoint(epoch=anchor_epoch, root=anchor_root)
    proposer_boost_root = Root()
    return Store(
        time=uint64(anchor_state.genesis_time + SLOT_DURATION_MS * anchor_state.slot // 1000),
        genesis_time=anchor_state.genesis_time,
        justified_checkpoint=justified_checkpoint,
        finalized_checkpoint=finalized_checkpoint,
        unrealized_justified_checkpoint=justified_checkpoint,
        unrealized_finalized_checkpoint=finalized_checkpoint,
        proposer_boost_root=proposer_boost_root,
        equivocating_indices=set(),
        blocks={anchor_root: copy(anchor_block)},
        block_states={anchor_root: copy(anchor_state)},
        block_timeliness={anchor_root: [True, True]},
        checkpoint_states={justified_checkpoint: copy(anchor_state)},
        unrealized_justifications={anchor_root: justified_checkpoint},
        payloads={},
        payload_timeliness_vote={},
        payload_data_availability_vote={},
        # [New in Heze:EIP7805]
        payload_inclusion_list_satisfaction={},
    )
```

### New `record_payload_inclusion_list_satisfaction`

*Note*: Payloads previously validated as satisfying the inclusion list
constraints SHOULD NOT be invalidated even if their associated `InclusionList`s
have subsequently been pruned.

*Note*: Whether a payload satisfies the inclusion list constraints MUST NOT
affect payload validation. A valid payload that fails to satisfy those
constraints remains valid, but fork choice does not extend it.

```python
def record_payload_inclusion_list_satisfaction(
    store: Store,
    state: BeaconState,
    root: Root,
    payload: ExecutionPayload,
    execution_engine: ExecutionEngine,
) -> None:
    inclusion_list_transactions = get_inclusion_list_transactions(
        get_inclusion_list_store(), state, Slot(state.slot - 1)
    )
    is_inclusion_list_satisfied = execution_engine.is_inclusion_list_satisfied(
        payload, inclusion_list_transactions
    )
    store.payload_inclusion_list_satisfaction[root] = is_inclusion_list_satisfied
```

### New `is_payload_inclusion_list_satisfied`

```python
def is_payload_inclusion_list_satisfied(store: Store, root: Root) -> bool:
    """
    Return whether the execution payload for the beacon block with root ``root``
    satisfied the inclusion list constraints, and was locally determined to be available.
    """
    # The beacon block root must be known
    assert root in store.payload_inclusion_list_satisfaction

    # If the payload is not locally available, the payload
    # is not considered to satisfy the inclusion list constraints
    if not is_payload_verified(store, root):
        return False

    return store.payload_inclusion_list_satisfaction[root]
```

### Modified `should_extend_payload`

*Note*: `should_extend_payload` is modified to not extend a payload if it does
not satisfy the inclusion list constraints.

```python
def should_extend_payload(store: Store, root: Root) -> bool:
    assert store.blocks[root].slot + 1 == get_current_slot(store)
    if not is_payload_verified(store, root):
        return False
    # [New in Heze:EIP7805]
    if not is_payload_inclusion_list_satisfied(store, root):
        return False
    proposer_root = store.proposer_boost_root
    payload_is_timely = payload_timeliness(store, root, timely=True)
    payload_data_is_available = payload_data_availability(store, root, available=True)
    return (
        (payload_is_timely and payload_data_is_available)
        or proposer_root == Root()
        or store.blocks[proposer_root].parent_root != root
        or is_parent_node_full(store, store.blocks[proposer_root])
    )
```

### New `get_inclusion_list_due_ms`

```python
def get_inclusion_list_due_ms() -> uint64:
    return get_slot_component_duration_ms(INCLUSION_LIST_DUE_BPS)
```

### Modified `get_checkpoint_block`

*Note*: `get_checkpoint_block` is modified to resolve the checkpoint to the last
block of the previous epoch (the epoch boundary) rather than the first block of
`epoch`, matching the modified FFG target. The genesis epoch, which has no
previous epoch, resolves to the genesis block.

```python
def get_checkpoint_block(store: Store, root: Root, epoch: Epoch) -> Root:
    """
    Compute the checkpoint block for epoch ``epoch`` in the chain of block ``root``
    """
    # [Modified in Heze:EIPXXXX]
    checkpoint_slot = get_checkpoint_slot(epoch)
    node = ForkChoiceNode(root=root, payload_status=PAYLOAD_STATUS_PENDING)
    return get_ancestor(store, node, checkpoint_slot).root
```

## Handlers

### New `on_inclusion_list`

*Note*: A new handler `on_inclusion_list` is called whenever an inclusion list
is received. Any call to this handler that triggers an unhandled exception
(e.g., a failed assert or an out-of-range list access) is considered invalid and
MUST NOT modify `store`.

```python
def on_inclusion_list(store: Store, signed_inclusion_list: SignedInclusionList) -> None:
    """
    Run ``on_inclusion_list`` upon receiving a new inclusion list.
    """
    inclusion_list = signed_inclusion_list.message

    seconds_since_genesis = store.time - store.genesis_time
    time_into_slot_ms = seconds_to_milliseconds(seconds_since_genesis) % SLOT_DURATION_MS
    inclusion_list_due_ms = get_inclusion_list_due_ms()
    is_timely = time_into_slot_ms < inclusion_list_due_ms

    process_inclusion_list(get_inclusion_list_store(), inclusion_list, is_timely)
```

### Modified `on_block`

*Note*: `on_block` is modified so that the finalized-slot optimization uses the
Heze checkpoint slot. This allows the first block of a finalized epoch to build
on the finalized checkpoint block, which is the last block before the epoch. It
is also modified to support a trusted anchor state that has already been dialed
to the start of the checkpoint epoch.

```python
def on_block(store: Store, signed_block: SignedBeaconBlock) -> None:
    """
    Run ``on_block`` upon receiving a new block.
    """
    block = signed_block.message
    # Parent block must be known
    assert block.parent_root in store.block_states

    # If this block builds on the parent's full payload, that payload must
    # have been verified by on_execution_payload_envelope
    if is_parent_node_full(store, block):
        assert is_payload_verified(store, block.parent_root)

    # Blocks cannot be in the future. If they are, their consideration must be delayed until they are in the past.
    current_slot = get_current_slot(store)
    assert current_slot >= block.slot

    # Check that block is later than the finalized checkpoint slot (optimization to reduce calls to get_ancestor)
    # [Modified in Heze:EIPXXXX]
    finalized_slot = get_checkpoint_slot(store.finalized_checkpoint.epoch)
    assert block.slot > finalized_slot
    # Check block is a descendant of the finalized block at the checkpoint finalized slot
    finalized_checkpoint_block = get_checkpoint_block(
        store,
        block.parent_root,
        store.finalized_checkpoint.epoch,
    )
    assert store.finalized_checkpoint.root == finalized_checkpoint_block

    # Make a copy of the state to avoid mutability issues
    state = copy(store.block_states[block.parent_root])

    # Check the block is valid and compute the post-state.
    #
    # [Modified in Heze:EIPXXXX]
    # The trusted anchor state may already be processed through the start of the
    # checkpoint epoch. If the next block is in that same slot, skip the
    # state_transition() slot-processing wrapper and process the block directly.
    block_root = hash_tree_root(block)
    if state.slot == block.slot:
        assert verify_block_signature(state, signed_block)
        process_block(state, block)
        assert block.state_root == hash_tree_root(state)
    else:
        state_transition(state, signed_block, validate_result=True)

    # Compute head before applying the block
    head = get_head(store)
    # Add new block to the store
    store.blocks[block_root] = block
    # Add new state for this block to the store
    store.block_states[block_root] = state
    # Add a new PTC voting for this block to the store
    store.payload_timeliness_vote[block_root] = [None] * PTC_SIZE
    store.payload_data_availability_vote[block_root] = [None] * PTC_SIZE

    # Notify the store about the payload_attestations in the block
    notify_ptc_messages(store, state, block.body.payload_attestations)

    record_block_timeliness(store, block_root)
    update_proposer_boost_root(store, head.root, block_root)

    # Update checkpoints in store if necessary
    update_checkpoints(store, state.current_justified_checkpoint, state.finalized_checkpoint)

    # Eagerly compute unrealized justification and finality.
    compute_pulled_up_tip(store, block_root)
```

### Modified `on_execution_payload_envelope`

```python
def on_execution_payload_envelope(
    store: Store, signed_envelope: SignedExecutionPayloadEnvelope
) -> None:
    """
    Run ``on_execution_payload_envelope`` upon receiving a new execution payload envelope.
    """
    envelope = signed_envelope.message
    # The corresponding beacon block root needs to be known
    assert envelope.beacon_block_root in store.block_states

    # Check if blob data is available
    # If not, this payload MAY be queued and subsequently considered when blob data becomes available
    assert is_data_available(envelope.beacon_block_root)

    state = store.block_states[envelope.beacon_block_root]

    # Verify the execution payload envelope
    verify_execution_payload_envelope(state, signed_envelope, EXECUTION_ENGINE)

    # [New in Heze:EIP7805]
    # Check if this payload satisfies the inclusion list constraints
    # If not, add this payload to the store as inclusion list constraints unsatisfied
    record_payload_inclusion_list_satisfaction(
        store, state, envelope.beacon_block_root, envelope.payload, EXECUTION_ENGINE
    )

    # Add execution payload envelope to the store
    store.payloads[envelope.beacon_block_root] = envelope
```
