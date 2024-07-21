import re
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Dict,
    FrozenSet,
    Generator,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

from interegular.fsm import (
    FSM,
    Alphabet,
    OblivionError,
    State,
    TransitionKey,
    _AnythingElseCls,
    anything_else,
)

from .outlines_core_rs import (  # noqa: F401
    FSMInfo,
    _walk_fsm,
    create_fsm_index_end_to_end,
    get_token_transition_keys,
    get_vocabulary_transition_keys,
    state_scan_tokens,
)

if TYPE_CHECKING:
    from outlines_core.models.tokenizer import Tokenizer


class BetterAlphabet(Alphabet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert anything_else in self._symbol_mapping
        self.anything_value = self._symbol_mapping[anything_else]

    def __getitem__(self, item):
        return self._symbol_mapping.get(item, self.anything_value)

    def copy(self):
        return BetterAlphabet(self._symbol_mapping.copy())


class BetterFSM(FSM):
    flat_transition_map: Dict[Tuple[int, int], int]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not isinstance(self.alphabet, BetterAlphabet):
            self.__dict__["alphabet"] = BetterAlphabet(self.alphabet._symbol_mapping)

        flat_transition_map = {}
        for from_state, trans_map in self.map.items():
            for trans_key, to_state in trans_map.items():
                flat_transition_map[(from_state, trans_key)] = to_state

        self.__dict__["flat_transition_map"] = flat_transition_map
        self.__dict__["_fsm_info"] = None

    def copy(self):
        return BetterFSM(
            alphabet=self.alphabet.copy(),
            states=self.states.copy(),
            initial=self.initial,
            finals=self.finals.copy(),
            map=self.map.copy(),
            __no_validation__=True,
        )

    @property
    def fsm_info(self):
        if self._fsm_info is None:
            anything_value = self.alphabet.anything_value
            self.__dict__["_fsm_info"] = FSMInfo(
                self.initial,
                self.finals,
                self.flat_transition_map,
                anything_value,
                # TODO FIXME: Perform this conversion in Rust?
                {
                    k: v
                    for k, v in self.alphabet._symbol_mapping.items()
                    if k != anything_else
                },
            )

        return self._fsm_info


TransitionTrie = Dict[TransitionKey, "Union[TransitionTrie, State, None]"]


def add_to_transition_trie(
    trie: TransitionTrie,
    key_seq: Sequence[TransitionKey],
    value: Union[State, None],
):
    for key in key_seq[:-1]:
        trie = cast(TransitionTrie, trie.setdefault(key, {}))
        assert isinstance(trie, dict), "key sequence of incompatible length"
    trie[key_seq[-1]] = value


# merge default_trie into the trie, only updating entries not present in the trie
def transition_trie_setdefault(
    trie: TransitionTrie,
    default_trie: TransitionTrie,
):
    for key, default_value in default_trie.items():
        dest_value = trie.get(key)
        if isinstance(dest_value, dict) and isinstance(default_value, dict):
            transition_trie_setdefault(dest_value, default_value)
        elif key not in trie:
            trie[key] = default_value


def byte_symbol(byte: int) -> str:
    return f"\x00{byte:02X}" if byte >= 0x80 else chr(byte)


def make_byte_level_fsm(
    fsm: FSM, keep_utf8: bool = False, frozen_tokens: List[str] = []
) -> FSM:
    """Convert an FSM to a byte-level FSM, expanding multi-byte characters as
    sequences of single-byte transitions.

    Parameters
    ----------
    fsm: (`interegular.FSM`):
        The token-level FSM to convert to a byte-level FSM.
    keep_utf8: (`bool`, *optional*):
        If set to True, the original utf-8 characters are kept as-is. Defaults to
        False. NOTE: we're representing bytes as strings to keep it type-compatible.
    frozen_tokens: (`List[str]`, *optional*):
        A list of tokens that should be kept as-is in the byte-level FSM. That is,
        these tokens will not be expanded into byte-level transitions. Defaults to
        an empty list.

    Returns
    -------
    `interegular.FSM`: A byte-level FSM.
    """

    anything_else_key = fsm.alphabet[anything_else]
    symbol_mapping: Dict[Union[str, _AnythingElseCls], TransitionKey] = {}
    map: Dict[State, Dict[TransitionKey, State]] = {}
    states: List[State] = list(fsm.states)

    # identify all multi-byte characters in the alphabet and build a mapping
    # from the original transition keys to sequences of new keys for each byte
    key_to_key_seqs: Dict[TransitionKey, Set[Tuple[TransitionKey, ...]]] = {}
    all_key_seqs: Set[Tuple[TransitionKey, ...]] = set()
    all_bytes: Set[int] = set()
    max_key = max(fsm.alphabet.values())
    for symbol, transition_key in fsm.alphabet.items():
        assert symbol == anything_else or symbol in frozen_tokens or len(symbol) == 1
        if symbol == anything_else or symbol in frozen_tokens or ord(symbol) < 0x80:
            symbol_mapping[symbol] = transition_key
        else:
            if keep_utf8:
                symbol_mapping[symbol] = transition_key
            key_list: List[TransitionKey] = []
            for byte in symbol.encode("utf-8"):
                symbol = byte_symbol(byte)
                if symbol not in symbol_mapping:
                    symbol_mapping[symbol] = max_key = TransitionKey(max_key + 1)
                    all_bytes.add(byte)
                key_list.append(symbol_mapping[symbol])
            key_seq = tuple(key_list)
            key_to_key_seqs.setdefault(transition_key, set()).add(key_seq)
            all_key_seqs.add(key_seq)

    # add all remaining multi-byte utf-8 bytes to the alphabet
    # (this is required to represent `anything_else`)
    utf8_ranges = {
        1: (0x80, 0xC0),  # continuation bytes
        2: (0xC0, 0xE0),  # 2-byte sequences
        3: (0xE0, 0xF0),  # 3-byte sequences
        4: (0xF0, 0xF8),  # 4-byte sequences
    }
    utf8_all_keys: Dict[int, Set[TransitionKey]] = {
        n: set() for n in utf8_ranges.keys()
    }
    for n, (start, end) in utf8_ranges.items():
        range_key = max_key = TransitionKey(max_key + 1)
        for byte in range(start, end):
            byte_key = symbol_mapping.setdefault(byte_symbol(byte), range_key)
            utf8_all_keys[n].add(byte_key)

    # cache of intermediate transition states by transitions from that state
    state_cache: Dict[FrozenSet[Tuple[TransitionKey, State]], State] = {}

    # helper function to create multi-step transitions between states
    max_state = max(fsm.states)

    def create_seq_transitions(
        seq_transitions_trie: TransitionTrie,
    ) -> Dict[TransitionKey, State]:
        nonlocal max_state
        result: Dict[TransitionKey, State] = {}

        for next_key, next_trie in seq_transitions_trie.items():
            if isinstance(next_trie, dict):
                next_transitions = create_seq_transitions(next_trie)
                if not next_transitions:
                    continue
                cache_key = frozenset(next_transitions.items())
                next_state = state_cache.get(cache_key)
                if next_state is None:
                    next_state = max_state = State(max_state + 1)
                    map[next_state] = next_transitions
                    state_cache[cache_key] = next_state
                    states.append(next_state)
                result[next_key] = next_state
            elif next_trie is not None:
                result[next_key] = next_trie

        return result

    # create new states and transitions
    for state, transitions in fsm.map.items():
        seq_transitions_trie: TransitionTrie = {}
        state_map: Dict[TransitionKey, State] = {}

        for transition_key, to_state in transitions.items():
            if transition_key in key_to_key_seqs:
                if keep_utf8:
                    state_map[transition_key] = to_state
                for key_seq in key_to_key_seqs[transition_key]:
                    add_to_transition_trie(seq_transitions_trie, key_seq, to_state)
            else:  # keep single-byte transitions as is
                state_map[transition_key] = to_state

        # handle multi-byte anything_else sequences
        if anything_else_key in transitions:
            for key_seq in all_key_seqs:
                add_to_transition_trie(seq_transitions_trie, key_seq, None)

            anything_else_trie: TransitionTrie = {}
            cont_trie: Union[TransitionTrie, State] = transitions[anything_else_key]
            for n in range(2, 5):
                cont_trie = {key: cont_trie for key in utf8_all_keys[1]}
                for key in utf8_all_keys[n]:
                    anything_else_trie[key] = cont_trie

            transition_trie_setdefault(seq_transitions_trie, anything_else_trie)

        # create new states and transitions
        next_transitions = create_seq_transitions(seq_transitions_trie)
        state_map.update(next_transitions)
        map[state] = state_map

    return FSM(
        alphabet=Alphabet(symbol_mapping),
        states=states,
        initial=fsm.initial,
        finals=fsm.finals,
        map=map,
    )


def make_byte_level_better_fsm(fsm: BetterFSM, keep_utf8=False) -> BetterFSM:
    new_fsm = make_byte_level_fsm(fsm, keep_utf8)
    return BetterFSM(
        alphabet=BetterAlphabet(new_fsm.alphabet._symbol_mapping),
        states=new_fsm.states,
        initial=new_fsm.initial,
        finals=new_fsm.finals,
        map=new_fsm.map,
    )


def make_deterministic_fsm(fsm: FSM) -> Tuple[BetterFSM, Dict[int, int]]:
    """Construct an equivalent FSM with deterministic state labels."""
    old_to_new_trans_keys = {
        trans_key: i
        for i, (trans_key, _) in enumerate(
            sorted(fsm.alphabet.by_transition.items(), key=lambda x: sorted(x[1]))
        )
    }

    new_symbol_mapping = {
        symbol: old_to_new_trans_keys[trans_key]
        for symbol, trans_key in fsm.alphabet._symbol_mapping.items()
    }

    new_alphabet = BetterAlphabet(new_symbol_mapping)

    new_map = {
        from_state: {
            old_to_new_trans_keys[trans_key]: to_state
            for trans_key, to_state in trans_map.items()
        }
        for from_state, trans_map in fsm.map.items()
    }

    old_to_new_states = {}
    old_to_new_states[fsm.initial] = 0

    i = 0
    seen = {fsm.initial}
    old_state_queue = [fsm.initial]
    while old_state_queue:
        old_state = old_state_queue.pop(-1)
        transitions = new_map[old_state]
        sorted_transitions = sorted(transitions.items(), key=lambda v: v[0])
        for _, old_state in sorted_transitions:
            if old_state not in seen:
                old_state_queue.append(old_state)
                seen.add(old_state)
            if old_state not in old_to_new_states:
                i += 1
                old_to_new_states[old_state] = i

    new_map = dict(
        sorted(
            (
                (
                    old_to_new_states[from_state],
                    dict(
                        sorted(
                            (
                                (trans_key, old_to_new_states[to_state])
                                for trans_key, to_state in trans_map.items()
                            ),
                            key=lambda v: v[0],
                        )
                    ),
                )
                for from_state, trans_map in new_map.items()
            ),
            key=lambda v: v[0],
        )
    )

    new_initial = 0
    new_finals = frozenset(
        sorted(old_to_new_states[old_state] for old_state in fsm.finals)
    )
    new_states = frozenset(sorted(new_map.keys()))

    new_fsm = BetterFSM(new_alphabet, new_states, new_initial, new_finals, new_map)

    return new_fsm, old_to_new_states


def walk_fsm(
    fsm: BetterFSM,
    token_transition_keys: Sequence[int],
    start_state: int,
    full_match: bool = True,
) -> List[int]:
    fsm_finals = fsm.finals

    state = start_state
    accepted_states: List[int] = []
    last_final_idx: int = 0

    fsm_transitions = fsm.flat_transition_map

    # Iterate over token transition key sequence. The transition key
    # sequence represents the FSM traversal rules of the tokens symbols.
    for i, trans_key in enumerate(token_transition_keys):
        new_state = fsm_transitions.get((state, trans_key))

        if new_state is None:
            if not full_match and last_final_idx > 0:
                return accepted_states[:last_final_idx]

            return []

        state = new_state

        if state in fsm_finals:
            last_final_idx = i + 1

        accepted_states.append(state)

    if full_match and last_final_idx - 1 != i:
        return []

    return accepted_states


def fsm_union(
    fsms: Sequence[FSM],
) -> Tuple[FSM, Dict[int, Tuple[Set[Tuple[int, int]], Set[int], Dict[int, Set[int]]]]]:
    """Construct an FSM representing the union of the FSMs in `fsms`.

    This is an updated version of `interegular.fsm.FSM.union` made to return an
    extra map of component FSMs to the sets of state transitions that
    correspond to them in the new FSM.

    """

    alphabet, new_to_old = Alphabet.union(*[fsm.alphabet for fsm in fsms])

    indexed_fsms = tuple(enumerate(fsms))

    initial = {i: fsm.initial for (i, fsm) in indexed_fsms}

    # Dedicated function accepting a "superset" and returning the next
    # "superset" obtained by following this transition in the new FSM
    def follow(current_state, new_transition: int):
        next = {}
        for i, f in indexed_fsms:
            old_transition = new_to_old[i][new_transition]
            if (
                i in current_state
                and current_state[i] in f.map
                and old_transition in f.map[current_state[i]]
            ):
                next[i] = f.map[current_state[i]][old_transition]
        if not next:
            raise OblivionError
        return next

    states = [initial]
    finals: Set[int] = set()
    map: Dict[int, Dict[int, int]] = {}

    # Map component FSMs to their new state-to-state transitions, finals, and a
    # map translating component FSM states to aggregate FSM states
    fsms_to_trans_finals: Dict[
        int, Tuple[Set[Tuple[int, int]], Set[int], Dict[int, Set[int]]]
    ] = {}

    i = 0
    while i < len(states):
        state = states[i]

        # Add to the finals of the aggregate FSM whenever we hit a final in a
        # component FSM
        if any(state.get(j, -1) in fsm.finals for (j, fsm) in indexed_fsms):
            finals.add(i)

        # Compute the map for this state
        map[i] = {}
        for transition in alphabet.by_transition:
            try:
                next = follow(state, transition)
            except OblivionError:
                # Reached an oblivion state; don't list it
                continue
            else:
                try:
                    # TODO: Seems like this could--and should--be avoided
                    j = states.index(next)
                except ValueError:
                    j = len(states)
                    states.append(next)

                map[i][transition] = j

                for fsm_id, fsm_state in next.items():
                    (
                        fsm_transitions,
                        fsm_finals,
                        fsm_old_to_new,
                    ) = fsms_to_trans_finals.setdefault(fsm_id, (set(), set(), {}))
                    old_from = state[fsm_id]
                    old_to = fsm_state
                    fsm_old_to_new.setdefault(old_from, set()).add(i)
                    fsm_old_to_new.setdefault(old_to, set()).add(j)
                    fsm_transitions.add((i, j))
                    if fsm_state in fsms[fsm_id].finals:
                        fsm_finals.add(j)

        i += 1

    fsm = FSM(
        alphabet=alphabet,
        states=range(len(states)),
        initial=0,
        finals=finals,
        map=map,
        __no_validation__=True,
    )

    fsm, old_to_new_states = make_deterministic_fsm(fsm)
    _fsms_to_trans_finals = {
        fsm_id: (
            {(old_to_new_states[s1], old_to_new_states[s2]) for s1, s2 in transitions},
            {old_to_new_states[s] for s in finals},
            {
                old_state: {old_to_new_states[new_state] for new_state in new_states}
                for old_state, new_states in old_to_new.items()
            },
        )
        for fsm_id, (transitions, finals, old_to_new) in sorted(
            fsms_to_trans_finals.items(), key=lambda x: x[0]
        )
    }

    return (
        fsm,
        _fsms_to_trans_finals,
    )


def get_sub_fsms_from_seq(
    state_seq: Sequence[int],
    fsms_to_trans_finals: Dict[
        int, Tuple[Set[Tuple[int, int]], Set[int], Dict[int, Set[int]]]
    ],
) -> Generator[Tuple[int, bool, bool], None, None]:
    """Get the indices of the sub-FSMs in `fsm` that could have matched the state sequence `state_seq`.

    Parameters
    ----------
    state_seq
        A state sequence.
    fsms_to_trans_finals
        A map from FSM indices to tuples containing sets of their state transitions
        and sets of the final/accept states.

    Returns
    -------
    A generator returning tuples containing each sub-FSM index (in the order
    they were union-ed to construct `fsm`) and booleans indicating whether or
    not there is another valid transition from the last state in the sequence
    for the associated sub-FSM (i.e. if the FSM can continue
    accepting/matching) and whether or not the sequence ends in a final state
    of the sub-FSM.
    """
    state_seq_transitions = set(zip(state_seq[:-1], state_seq[1:]))
    last_fsm_state = state_seq[-1]
    yield from (
        (
            # The sub-FMS index
            fsm_idx,
            # Is there another possible transition in this sub-FSM?
            any(last_fsm_state == from_s for (from_s, to_s) in transitions),
            # Is this sub-FSM in a final state?
            state_seq[-1] in finals,
        )
        for fsm_idx, (transitions, finals, _) in fsms_to_trans_finals.items()
        if state_seq_transitions.issubset(transitions)
    )


re_llama_byte_token = re.compile(r"^<0x[0-9A-F]{2}>$")

# The "▁*" prefix is required to handle Gemma and GPT-SW3 tokenizers, and the "\.*"
# suffix is required to handle the NorwAI tokenizer.
re_replacement_seq = re.compile(r"^▁*�+\.*$")


# Copied from transformers.models.gpt2.tokenization_gpt2.bytes_to_unicode
@lru_cache()
def gpt2_bytes_to_unicode():
    """
    Returns list of utf-8 byte and a mapping to unicode strings. We specifically avoids mapping to whitespace/control
    characters the bpe code barfs on.

    The reversible bpe codes work on unicode strings. This means you need a large # of unicode characters in your vocab
    if you want to avoid UNKs. When you're at something like a 10B token dataset you end up needing around 5K for
    decent coverage. This is a significant percentage of your normal, say, 32K bpe vocab. To avoid that, we want lookup
    tables between utf-8 bytes and unicode strings.
    """
    bs = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(2**8):
        if b not in bs:
            bs.append(b)
            cs.append(2**8 + n)
            n += 1
    cs = [chr(n) for n in cs]
    return dict(zip(bs, cs))


@lru_cache()
def gpt2_unicode_to_bytes():
    return {v: k for k, v in gpt2_bytes_to_unicode().items()}


@lru_cache
def reduced_vocabulary(
    tokenizer: "Tokenizer",
) -> Tuple[Dict[str, List[int]], Set[int]]:
    """Create a map from decoded vocabulary tokens to lists of equivalent token ids."""
    # TODO FIXME: See if we can get the underlying Rust tokenizers from HF and
    # do all this in Rust
    empty_token_ids = set()
    vocabulary: Dict[str, List[int]] = {}
    for token, token_idx in tokenizer.vocabulary.items():
        if token in tokenizer.special_tokens:
            continue

        token_str: Union[str, Tuple[str, ...]] = tokenizer.convert_token_to_string(
            token
        )

        if token_str:
            # invalid utf-8 sequences are replaced with � (\ufffd), but there
            # might also be tokens specifically for �, ��, ���, etc.
            if "\ufffd" in token_str and not re_replacement_seq.match(token):
                if re_llama_byte_token.match(token):
                    # llama-like tokenizers have <0xXX> tokens for all
                    # bytes >= 0x80 and represent all incomplete utf-8
                    # sequences using such tokens
                    token_bytes = [int(token[3:5], 16)]
                else:
                    # gpt2-like tokenizers have multi-byte tokens that can
                    # have a mix of full and incomplete utf-8 characters,
                    # for example, b` \xf0` can be one token; these tokenizers
                    # map each byte to a valid utf-8 character
                    token_bytes = cast(
                        List[int], [gpt2_unicode_to_bytes().get(c) for c in token]
                    )
                    if None in token_bytes:
                        raise RuntimeError(
                            f"Cannot convert token `{token}` ({token_idx}) to bytes: {token_str}"
                        )
                token_str = "".join(byte_symbol(b) for b in token_bytes)

            assert isinstance(token_str, str)

            vocabulary.setdefault(token_str, []).append(token_idx)
        else:
            empty_token_ids.add(token_idx)

    return vocabulary, empty_token_ids


def create_fsm_index_tokenizer(
    fsm: BetterFSM,
    tokenizer: "Tokenizer",
    frozen_tokens: Optional[Iterable[str]] = None,
) -> Tuple[Dict[int, Dict[int, int]], Set[int]]:
    """Construct an FMS index from a tokenizer.

    This uses the end-to-end approach of `create_fsm_index_end_to_end`.

    Parameters
    ----------
    fsm: (`BetterFSM`):
        A cache-friendly FSM. Other interegular FSMs can also be used, but caching
        may not work as expected.
    tokenizer: (`Tokenizer`):
        The model's tokenizer.
    frozen_tokens: (`List[str]`, *optional*):
        A list of tokens that should be kept as-is when expanding the token-level
        FSM into a byte-level FSM. Defaults to an empty list.

    Returns
    -------
    states_to_token_maps: (`Dict[int, Dict[int, int]]`):
        A mapping from states to a mapping from token ids originating from that state
        to the next state to transition to given that token. The structure is as follows:
        (origin_state -> (token_id -> next_state))
    empty_token_ids: (`Set[int]`):
        A set of token ids that correspond to empty strings.

    .. warning::

        `fsm` needs to be deterministically ordered so that future caching makes sense.
    """
    vocabulary, empty_token_ids = reduced_vocabulary(tokenizer)

    states_to_token_subsets = create_fsm_index_end_to_end(
        fsm.fsm_info,
        list(vocabulary.items()),
        frozenset(frozen_tokens) if frozen_tokens is not None else frozenset(),
    )

    # Allow transitions to EOS from all terminals FSM states that are
    # reachable
    # TODO: Do we really need this anymore?
    for state in fsm.fsm_info.finals:
        subset = states_to_token_subsets.get(state)
        if subset is not None:
            subset[tokenizer.eos_token_id] = state

    return states_to_token_subsets, empty_token_ids