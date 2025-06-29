# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Defines the JetStream API.

These functions are the accelerator functions which an outer sampling loop
could want to call, enabling interleaved (continuous batching) inference.
"""

import abc
from typing import Any, Optional, Tuple, Union, Callable

from flax import struct
import jax
import numpy as np
import uuid

from jetstream.engine import tokenizer_pb2
from jetstream.engine import token_utils


# The model parameters - their partitioning will be unique for different prefill
# and decode topoologies.
Params = Any
# The result of a prefill operation, often a batch size 1 KVCache.
Prefix = Any
# The inputs into a generation step, often a prefill and generate cache tuple.
DecodeState = Any
# Accelerator representation of tokens.
DeviceTokens = Any
# Cpus asscociated with the mesh.
CpuDevices = Any
# Tokenkizer used by the engine
Tokenizer = Any
# PRNG key used for prefilling
PRNGKeyType = Any


@struct.dataclass
class ExistingPrefix:
  cache: Any
  common_prefix_tokens: jax.Array


@struct.dataclass
class SlotData:
  """Class to store slot data."""

  tokens: Union[jax.Array, np.ndarray]
  valid: Union[jax.Array, np.ndarray]
  lengths: Union[jax.Array, np.ndarray]
  log_prob: Union[jax.Array, np.ndarray] = None


# pylint: disable=g-doc-args
@struct.dataclass
class ResultTokens(abc.ABC):
  """Class to store returned tokens in.

  We store everything in one array, and keep indexes - because copying
  a single array to host is much faster.
  Each tuple represents the indices of the relevant data.
  """

  # Shape: [batch, tokens.shape[1] + validity.shape[1] + lengths.shape[1]]
  data: Union[jax.Array, np.ndarray]
  # The range of indices which contain tokens.
  tokens_idx: tuple[int, int] = struct.field(
      pytree_node=False,
  )
  # The range of indices which contain the validity of
  # the tokens.
  valid_idx: tuple[int, int] = struct.field(
      pytree_node=False,
  )
  # The range of indices which contain the lengths up till now of the lengths
  # of each generated sequence.
  length_idx: tuple[int, int] = struct.field(
      pytree_node=False,
  )
  samples_per_slot: int = struct.field(
      pytree_node=False,
  )
  # log probabilities of the tokens. Shape: [batch, tokens]
  log_prob: Union[jax.Array, np.ndarray] = struct.field(
      default=None,
  )

  def copy_to_host_async(self: "ResultTokens") -> None:
    """Copy to host asynchronously."""
    # Do nothing for np array
    if isinstance(self.data, np.ndarray):
      return
    self.data.copy_to_host_async()

  def convert_to_numpy(self: "ResultTokens") -> "ResultTokens":
    """Converts to numpy."""
    return ResultTokens(
        np.array(self.data),
        self.tokens_idx,
        self.valid_idx,
        self.length_idx,
        self.samples_per_slot,
        self.log_prob,
    )

  def get_result_at_slot(self, slot: int) -> SlotData:
    """Returns the token at a given slot.

    Args:
      slot: An integer from [0, n) representing an index into the batch.

    Note: implementations of this method must correctly handle
    microbatches, if microbatches are used.
    """
    # Potentially get multiple beams for given slot.
    start_idx = slot * self.samples_per_slot
    end_idx = (slot + 1) * self.samples_per_slot
    # Mask out any non valid tokens.
    return SlotData(
        tokens=self.data[
            start_idx:end_idx, self.tokens_idx[0] : self.tokens_idx[1]
        ],
        valid=self.data[
            start_idx:end_idx, self.valid_idx[0] : self.valid_idx[1]
        ],
        # Only get a 1D representation here
        lengths=self.data[
            start_idx:end_idx, self.length_idx[0] : self.length_idx[1]
        ][:, 0],
    )

  def get_result_at_slots(self, slots: tuple[int]) -> SlotData:
    """Returns the tokens at given slots.

    Args:
      slots: a tuple of integers from [0, n) representing indices
      into the batch.

    """
    return SlotData(
        tokens=self.data[slots, self.tokens_idx[0] : self.tokens_idx[1]],
        valid=self.data[slots, self.valid_idx[0] : self.valid_idx[1]],
        # Only get a 1D representation here
        lengths=self.data[slots, self.length_idx[0] : self.length_idx[1]][:, 0],
        log_prob=self.log_prob[slots, :] if self.log_prob is not None else None,
    )


class Engine(abc.ABC):
  """The computational core of the generative model server.

  Engine defines an API that models must adhere to as they plug into the
  JetStream efficient serving infrastructure.
  """

  @abc.abstractmethod
  def prefill(
      self,
      *,
      params: Params,
      existing_prefix: Optional[ExistingPrefix] = None,
      padded_tokens: jax.Array,
      true_length: int,
      sampler: Optional[Callable[[Any], Any]] = None,
      request_id: Optional[uuid.UUID] = None,
  ) -> Tuple[Prefix, ResultTokens]:
    """Computes a kv-cache for a set of tokens conditional on existing cache.

    existing_prefix (if provided) represents a prefix that has already been
    processed by the underlying model. tokens is logically appended
    to the text represented by `existing_prefix`. This method returns a new
    kv_cache (typically) for the resulting text.

    If sampler is passed, then the engine should use it do sample next token.
    """

  @abc.abstractmethod
  def prefill_multisampling(
      self,
      *,
      params: Params,
      existing_prefix: Optional[jax.Array] = None,
      padded_tokens: jax.Array,
      true_length: int,
      sampler: Optional[Callable[[Any], Any]] = None,  # pylint: disable=unused-argument
      rng: Optional[PRNGKeyType] = None,
      num_samples: int = 1,
  ) -> Tuple[Prefix, ResultTokens]:
    """Computes a kv-cache for a new generate request.

    With multi-sampling, the engine will generate multiple first tokens in the
    prefilling stage. The number of tokens is specified by num_samples.
    """

  @abc.abstractmethod
  def generate(
      self,
      params: Params,
      decode_state: DecodeState,
      sampler: Optional[Callable[[Any], Any]] = None,
  ) -> Tuple[DecodeState, ResultTokens]:
    """Generates tokens for each sequence being decoded in parallel.

    Generate takes a batch of pre-computed kv-caches, and computes:
      - the predicted next token for each of the sequences
      - an updated set of kv-caches

    In the case of pipelining, this will handle N cycles (where each cycle
    consists of each microbatch progressing through every stage), in
    non-pipelined code this is a full forward pass. In both cases, this accounts
    for a full embed-layerstack-unembed-sample operation.

    If sampler is passed, then the engine should use it do sample next token.
    """

  @abc.abstractmethod
  def insert(
      self,
      prefix: Prefix,
      decode_state: DecodeState,
      slot: int,
      request_id: Optional[uuid.UUID] = None,
  ) -> DecodeState:
    """Adds `new_request` into `caches` at 'slot'.

    When decoding multiple requests in parallel, when one request finishes, a
    new request must be slotted into the recently vacated spot: `insert`!

    This can occur in between and async to generate calls, and takes a lock over
    that row of the cache.

    The slot may represent a tuple of positions (e.g. microbatch, pipeline stage
    and batch), but at the engine interface level all of these are exposed as
    a [0, n) range of slots and converted internally.
    """

  @abc.abstractmethod
  def bulk_insert(
      self,
      prefix: Prefix,
      decode_state: DecodeState,
      slots: list[int],
  ) -> DecodeState:
    """Insert a single computed prefill cache into multiple slots in
    KV cache.
    """

  def free_resource(
      self,
      slot: int,  # pylint: disable=unused-argument
  ) -> Any:
    """Free cache and other decode resource for the slot.

    This function is needed for advanced attetnion kenel like PageAttetion.
    After finishing one request, the engine need to free all used page block
    resource and reuse for coming requests.
    """
    return None

  @abc.abstractmethod
  def load_params(self, *args, **kwargs) -> Params:
    """Loads parameters.

    May not be used in full production form, where weights are part of the saved
    model.
    """

  @abc.abstractmethod
  def get_prefix_destination_sharding(self) -> Any:
    """Returns the shardings necessary to transfer data between engines."""

  @abc.abstractmethod
  def get_tokenizer(
      self,
  ) -> tokenizer_pb2.TokenizerParameters:
    """Returns the info to construct a tokenizer in py/c++."""

  def build_tokenizer(
      self,
      metadata: tokenizer_pb2.TokenizerParameters,
  ) -> Tokenizer:
    """Builds a new tokenizer object and returns it."""
    return token_utils.SentencePieceTokenizer(metadata)

  @abc.abstractmethod
  def init_decode_state(self, *args, **kwargs) -> DecodeState:
    """Initialises any state which a generation step transforms."""

  @property
  @abc.abstractmethod
  def max_concurrent_decodes(self) -> int:
    """Total capacity."""

  @property
  @abc.abstractmethod
  def samples_per_slot(self) -> int:
    """Total samples per slot."""

  @property
  @abc.abstractmethod
  def max_prefill_length(self) -> int:
    """Maximum prefill length."""

  @property
  @abc.abstractmethod
  def mesh(self) -> jax.sharding.Mesh:
    """Mesh which the engine is running on."""

  @property
  @abc.abstractmethod
  def colocated_cpus(self) -> Union[list[CpuDevices], None]:
    """CPU devices colocated with the engine's accelerators."""

  @property
  @abc.abstractmethod
  def use_chunked_prefill(self) -> bool:
    """Whether to use chunked prefill."""

  @property
  @abc.abstractmethod
  def prefill_chunk_size(self) -> int:
    """Prefill chunk size."""


class JetStreamEngine(Engine):
  """A wrapper engine of the Engine class.

  JetStreamEngine defines the warmed up model server engine.
  """

  def __init__(self, downstream_engine: Engine):
    self._downstream_engine = downstream_engine

    self.prefill_buckets = None
    self.warm = False

  def prefill(
      self,
      *,
      params: Params,
      existing_prefix: Optional[Prefix] = None,
      padded_tokens: jax.Array,
      true_length: int,
  ) -> Tuple[Prefix, ResultTokens]:

    prefill_result, first_token = self._downstream_engine.prefill(
        params=params,
        padded_tokens=padded_tokens,
        true_length=true_length,
    )
    return prefill_result, first_token

  def prefill_multisampling(
      self,
      *,
      params: Params,
      existing_prefix: Optional[jax.Array] = None,
      padded_tokens: jax.Array,
      true_length: int,
      sampler: Optional[Callable[[Any], Any]] = None,  # pylint: disable=unused-argument
      rng: Optional[PRNGKeyType] = None,
      num_samples: int = 1,
  ) -> Tuple[Prefix, ResultTokens]:

    prefill_result, first_token = self._downstream_engine.prefill_multisampling(
        params=params,
        existing_prefix=existing_prefix,
        padded_tokens=padded_tokens,
        true_length=true_length,
        sampler=sampler,
        rng=rng,
        num_samples=num_samples,
    )
    return prefill_result, first_token

  def insert(
      self,
      prefix: Prefix,
      decode_state: DecodeState,
      slot: int,
      request_id: Optional[uuid.UUID] = None,
  ) -> DecodeState:

    decode_state = self._downstream_engine.insert(
        prefix=prefix,
        decode_state=decode_state,
        slot=slot,
        request_id=request_id,
    )
    return decode_state

  def bulk_insert(
      self,
      prefix: Prefix,
      decode_state: DecodeState,
      slots: list[int],
  ) -> DecodeState:

    decode_state = self._downstream_engine.bulk_insert(
        prefix=prefix,
        decode_state=decode_state,
        slots=slots,
    )
    return decode_state

  def generate(
      self, params: Params, decode_state: DecodeState
  ) -> Tuple[DecodeState, ResultTokens]:
    decode_state, sampled_tokens = self._downstream_engine.generate(
        params=params, decode_state=decode_state
    )
    return decode_state, sampled_tokens

  def load_params(self, *args, **kwargs) -> Params:
    return self._downstream_engine.load_params(*args, **kwargs)

  def get_prefix_destination_sharding(self) -> Any:
    return self._downstream_engine.get_prefix_destination_sharding()

  def get_tokenizer(
      self,
  ) -> tokenizer_pb2.TokenizerParameters:
    return self._downstream_engine.get_tokenizer()

  def build_tokenizer(
      self,
      metadata: tokenizer_pb2.TokenizerParameters,
  ) -> Tokenizer:
    """Builds a new tokenizer object and returns it."""
    return self._downstream_engine.build_tokenizer(metadata)

  def init_decode_state(self, *args, **kwargs) -> DecodeState:
    return self._downstream_engine.init_decode_state(*args, **kwargs)

  @property
  def max_concurrent_decodes(self) -> int:
    return self._downstream_engine.max_concurrent_decodes

  @property
  def samples_per_slot(self) -> int:
    return self._downstream_engine.samples_per_slot

  @property
  def max_prefill_length(self) -> int:
    return self._downstream_engine.max_prefill_length

  @property
  def mesh(self) -> jax.sharding.Mesh:
    return self._downstream_engine.mesh

  @property
  def colocated_cpus(self) -> Union[list[CpuDevices], None]:
    return self._downstream_engine.colocated_cpus

  @property
  def use_chunked_prefill(self) -> bool:
    return self._downstream_engine.use_chunked_prefill

  @property
  def prefill_chunk_size(self) -> int:
    return self._downstream_engine.prefill_chunk_size
