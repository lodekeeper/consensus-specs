# Heze -- The Beacon Chain

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Constants](#constants)
  - [Domains](#domains)
- [Preset](#preset)
  - [Inclusion list committee](#inclusion-list-committee)
- [Containers](#containers)
  - [New containers](#new-containers)
    - [`InclusionList`](#inclusionlist)
    - [`SignedInclusionList`](#signedinclusionlist)
- [Helpers](#helpers)
  - [Predicates](#predicates)
    - [New `is_valid_inclusion_list_signature`](#new-is_valid_inclusion_list_signature)
  - [Beacon state accessors](#beacon-state-accessors)
    - [New `get_inclusion_list_committee`](#new-get_inclusion_list_committee)
    - [New `get_checkpoint_root`](#new-get_checkpoint_root)
    - [Modified `get_attestation_participation_flag_indices`](#modified-get_attestation_participation_flag_indices)
- [Beacon chain state transition function](#beacon-chain-state-transition-function)
  - [Epoch processing](#epoch-processing)
    - [Justification and finalization](#justification-and-finalization)
      - [Modified `weigh_justification_and_finalization`](#modified-weigh_justification_and_finalization)

<!-- mdformat-toc end -->

## Introduction

Heze is a consensus-layer upgrade containing a number of features. Including:

- [EIP-7805](https://eips.ethereum.org/EIPS/eip-7805): Fork-choice enforced
  Inclusion Lists (FOCIL)
- EIP-XXXX (proposed): Anchor the FFG target/checkpoint to the epoch boundary
  block -- the last block of the previous epoch rather than the first block of
  the current epoch

## Constants

### Domains

| Name                              | Value                      |
| --------------------------------- | -------------------------- |
| `DOMAIN_INCLUSION_LIST_COMMITTEE` | `DomainType('0x10000000')` |

## Preset

### Inclusion list committee

| Name                            | Value                |
| ------------------------------- | -------------------- |
| `INCLUSION_LIST_COMMITTEE_SIZE` | `uint64(2**4)` (=16) |

## Containers

### New containers

#### `InclusionList`

```python
class InclusionList(Container):
    slot: Slot
    validator_index: ValidatorIndex
    inclusion_list_committee_root: Root
    transactions: List[Transaction, MAX_TRANSACTIONS_PER_PAYLOAD]
```

#### `SignedInclusionList`

```python
class SignedInclusionList(Container):
    message: InclusionList
    signature: BLSSignature
```

## Helpers

### Predicates

#### New `is_valid_inclusion_list_signature`

```python
def is_valid_inclusion_list_signature(
    state: BeaconState, signed_inclusion_list: SignedInclusionList
) -> bool:
    """
    Check if ``signed_inclusion_list`` has a valid signature.
    """
    message = signed_inclusion_list.message
    index = message.validator_index
    pubkey = state.validators[index].pubkey
    domain = get_domain(state, DOMAIN_INCLUSION_LIST_COMMITTEE, compute_epoch_at_slot(message.slot))
    signing_root = compute_signing_root(message, domain)
    return bls.Verify(pubkey, signing_root, signed_inclusion_list.signature)
```

### Beacon state accessors

#### New `get_inclusion_list_committee`

```python
def get_inclusion_list_committee(
    state: BeaconState, slot: Slot
) -> Vector[ValidatorIndex, INCLUSION_LIST_COMMITTEE_SIZE]:
    """
    Get the inclusion list committee for the given ``slot``.
    """
    epoch = compute_epoch_at_slot(slot)
    indices: List[ValidatorIndex] = []
    # Concatenate all committees for this slot in order
    committees_per_slot = get_committee_count_per_slot(state, epoch)
    for i in range(committees_per_slot):
        committee = get_beacon_committee(state, slot, CommitteeIndex(i))
        indices.extend(committee)
    return Vector[ValidatorIndex, INCLUSION_LIST_COMMITTEE_SIZE]([
        indices[i % len(indices)] for i in range(INCLUSION_LIST_COMMITTEE_SIZE)
    ])
```

#### New `get_checkpoint_root`

*Note*: `get_checkpoint_root` returns the block root anchoring the checkpoint
for `epoch`. Unlike `get_block_root`, which returns the block at the first slot
of `epoch`, this returns the block at the last slot of the previous epoch -- the
true epoch boundary. Since the boundary block is fully known one epoch in
advance, validators no longer race to identify a slot-0 block when constructing
the target vote. For the genesis epoch, which has no previous epoch, the genesis
block is returned.

```python
def get_checkpoint_root(state: BeaconState, epoch: Epoch) -> Root:
    """
    Return the block root anchoring the checkpoint for ``epoch`` -- the last
    block of the previous epoch (the epoch boundary).
    """
    return get_block_root_at_slot(state, get_checkpoint_slot(epoch))
```

#### Modified `get_attestation_participation_flag_indices`

*Note*: `get_attestation_participation_flag_indices` is modified to compute the
matching target via `get_checkpoint_root`, so that the target vote is satisfied
by the epoch boundary block (the last block of the previous epoch).

```python
def get_attestation_participation_flag_indices(
    state: BeaconState, data: AttestationData, inclusion_delay: uint64
) -> Sequence[int]:
    """
    Return the flag indices that are satisfied by an attestation.
    """
    # Matching source
    if data.target.epoch == get_current_epoch(state):
        justified_checkpoint = state.current_justified_checkpoint
    else:
        justified_checkpoint = state.previous_justified_checkpoint
    is_matching_source = data.source == justified_checkpoint

    # Matching target
    # [Modified in Heze:EIPXXXX]
    target_root = get_checkpoint_root(state, data.target.epoch)
    target_root_matches = data.target.root == target_root
    is_matching_target = is_matching_source and target_root_matches

    # [New in Gloas:EIP7732]
    if is_attestation_same_slot(state, data):
        assert data.index == 0
        payload_matches = True
    else:
        slot_index = data.slot % SLOTS_PER_HISTORICAL_ROOT
        payload_index = state.execution_payload_availability[slot_index]
        payload_matches = data.index == payload_index

    # Matching head
    head_root = get_block_root_at_slot(state, data.slot)
    head_root_matches = data.beacon_block_root == head_root
    # [Modified in Gloas:EIP7732]
    is_matching_head = is_matching_target and head_root_matches and payload_matches

    assert is_matching_source

    participation_flag_indices = []
    if is_matching_source and inclusion_delay <= integer_squareroot(SLOTS_PER_EPOCH):
        participation_flag_indices.append(TIMELY_SOURCE_FLAG_INDEX)
    if is_matching_target:
        participation_flag_indices.append(TIMELY_TARGET_FLAG_INDEX)
    if is_matching_head and inclusion_delay == MIN_ATTESTATION_INCLUSION_DELAY:
        participation_flag_indices.append(TIMELY_HEAD_FLAG_INDEX)

    return participation_flag_indices
```

## Beacon chain state transition function

### Epoch processing

#### Justification and finalization

##### Modified `weigh_justification_and_finalization`

*Note*: `weigh_justification_and_finalization` is modified so that the justified
checkpoints it records are anchored to the epoch boundary block via
`get_checkpoint_root`, matching the modified attestation target. Only the
checkpoint roots change; the justification and finalization accounting is
unchanged.

```python
def weigh_justification_and_finalization(
    state: BeaconState,
    total_active_balance: Gwei,
    previous_epoch_target_balance: Gwei,
    current_epoch_target_balance: Gwei,
) -> None:
    previous_epoch = get_previous_epoch(state)
    current_epoch = get_current_epoch(state)
    old_previous_justified_checkpoint = state.previous_justified_checkpoint
    old_current_justified_checkpoint = state.current_justified_checkpoint

    # Process justifications
    state.previous_justified_checkpoint = state.current_justified_checkpoint
    state.justification_bits[1:] = state.justification_bits[: JUSTIFICATION_BITS_LENGTH - 1]
    state.justification_bits[0] = 0b0
    if previous_epoch_target_balance * 3 >= total_active_balance * 2:
        # [Modified in Heze:EIPXXXX]
        state.current_justified_checkpoint = Checkpoint(
            epoch=previous_epoch, root=get_checkpoint_root(state, previous_epoch)
        )
        state.justification_bits[1] = 0b1
    if current_epoch_target_balance * 3 >= total_active_balance * 2:
        # [Modified in Heze:EIPXXXX]
        state.current_justified_checkpoint = Checkpoint(
            epoch=current_epoch, root=get_checkpoint_root(state, current_epoch)
        )
        state.justification_bits[0] = 0b1

    # Process finalizations
    bits = state.justification_bits
    # The 2nd/3rd/4th most recent epochs are justified, the 2nd using the 4th as source
    if all(bits[1:4]) and old_previous_justified_checkpoint.epoch + 3 == current_epoch:
        state.finalized_checkpoint = old_previous_justified_checkpoint
    # The 2nd/3rd most recent epochs are justified, the 2nd using the 3rd as source
    if all(bits[1:3]) and old_previous_justified_checkpoint.epoch + 2 == current_epoch:
        state.finalized_checkpoint = old_previous_justified_checkpoint
    # The 1st/2nd/3rd most recent epochs are justified, the 1st using the 3rd as source
    if all(bits[0:3]) and old_current_justified_checkpoint.epoch + 2 == current_epoch:
        state.finalized_checkpoint = old_current_justified_checkpoint
    # The 1st/2nd most recent epochs are justified, the 1st using the 2nd as source
    if all(bits[0:2]) and old_current_justified_checkpoint.epoch + 1 == current_epoch:
        state.finalized_checkpoint = old_current_justified_checkpoint
```
