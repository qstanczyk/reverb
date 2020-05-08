# Lint as: python3
# Copyright 2019 DeepMind Technologies Limited.
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

"""Replay client Python interface.

The ReverbClient is used primarily for feeding the ReplayService with new data.
The preferred method is to use the `Writer` as it allows for the most
flexibility.

Consider an example where we wish to generate all possible connected sequences
of length 5 based on a single actor.

```python

    client = Client(...)
    env = ....  # Construct the environment
    policy = ....  # Construct the agent's policy

    for episode in range(NUM_EPISODES):
      timestep = env.reset()
      step_within_episode = 0
      with client.writer(max_sequence_length=5) as writer:
        while not timestep.last():
          action = policy(timestep)
          new_timestep = env.step(action)

          # Add the observation of the state the agent when doing action, the
          # action it took and the reward it received.
          writer.append_timestep(
              (timestep.observation, action, new_timestep.reward))

          timestep = new_timestep
          step_within_episode += 1

          if step_within_episode >= 5:
            writer.create_prioritized_item(
                table='my_distribution',
                num_timesteps=5,
                priority=calc_priority(...))

        # Add an item for the sequence terminating in the final stage.
        if steps_within_episode >= 5:
          writer.create_prioritized_item(
              table='my_distribution',
              num_timesteps=5,
              priority=calc_priority(...))

```

If you do not want overlapping sequences but instead want to insert complete
trajectories then the `insert`-method should be used.

```python

    client = Client(...)

    trajectory_generator = ...
    for trajectory in trajectory_generator:
      client.insert(trajectory, {'my_distribution': calc_priority(trajectory)})

```

"""

from typing import Any, Dict, Generator, List, Optional

from absl import logging
from reverb import errors
from reverb import pybind
from reverb import replay_sample
from reverb import reverb_types
import tree

from reverb.cc import schema_pb2
from tensorflow.python.saved_model import nested_structure_coder  # pylint: disable=g-direct-tensorflow-import


class Writer:
  """Writer is used for streaming data of arbitrary length.

  See ReverbClient.writer for documentation.
  """

  def __init__(self, internal_writer: pybind.ReplayWriter):
    """Constructor for Writer (must only be called by ReverbClient.writer)."""
    self._writer = internal_writer
    self._closed = False

  def __enter__(self) -> 'Writer':
    if self._closed:
      raise ValueError('Cannot reuse already closed Writer')
    return self

  def __exit__(self, *_):
    self.close()

  def __del__(self):
    if not self._closed:
      logging.warning(
          'Writer-object deleted without calling .close explicitly.')

  def append_timestep(self, timestep: Any):
    """Appends a timestep to the internal buffer.

    NOTE: Calling this method alone does not result in anything being inserted
    into the replay. To trigger timestep insertion, `create_prioritized_item`
    must be called so that the resulting sequence includes the timestep.

    Consider the following example:

    ```python

        A, B, C = ...
        client = Client(...)

        with client.writer(max_sequence_length=2) as writer:
          writer.append_timestep(A)  # A is added to the internal buffer.
          writer.append_timestep(B)  # B is added to the internal buffer.

          # The buffer is now full so when this is called C is added and A is
          # removed from the internal buffer and since A was never referenced by
          # a prioritized item it was never sent to the server.
          writer.append_timestep(C)

          # A sequence of length 1 is created referencing only C and thus C is
          # sent to the server.
          writer.create_prioritized_item('my_table', 1, 5.0)

        # Writer is now closed and B was never referenced by a prioritized item
        # and thus never sent to the server.

    ```

    Args:
      timestep: The (possibly nested) structure to make available for new
        prioritized items to reference.
    """
    self._writer.AppendTimestep(tree.flatten(timestep))

  def create_prioritized_item(self, table: str, num_timesteps: int,
                              priority: float):
    """Creates a prioritized item and sends it to the ReplayService.

    This method is what effectively makes data available for sampling. See the
    docstring of `append_timestep` for an illustrative example of the behavior.

    Args:
      table: Name of the priority table to insert the item into.
      num_timesteps: The number of most recently added timesteps that the new
        item should reference.
      priority: The priority used for determining the sample probability of the
        new item.

    Raises:
      ValueError: If num_timesteps is < 1.
      StatusNotOk: If num_timesteps is > than the timesteps currently available
        in the buffer.
    """
    if num_timesteps < 1:
      raise ValueError('num_timesteps (%d) must be a positive integer')
    self._writer.AddPriority(table, num_timesteps, priority)

  def close(self):
    """Closes the stream to the ReplayService.

    The method is automatically called when existing the contextmanager scope.

    Note: Writer-object must be abandoned after this method called.

    Raises:
      ValueError: If already has been called.
    """
    if self._closed:
      raise ValueError('close() has already been called on Writer.')
    self._closed = True
    self._writer.Close()


class Client:
  """Client for interacting with a Reverb ReplayService from Python.

  Note: This client should primarily be used when inserting data or prototyping
  at very small scale.
  Whenever possible, prefer to use TFClient (see ./tf_client.py).
  """

  def __init__(self, server_address: str, client: pybind.ReplayClient = None):
    """Constructor of ReverbClient.

    Args:
      server_address: Address to the Reverb ReplayService.
      client: Optional pre-existing ReplayClient. For internal use only.
    """
    self._server_address = server_address
    self._client = client if client else pybind.ReplayClient(server_address)

  def __reduce__(self):
    return self.__class__, (self._server_address,)

  @property
  def server_address(self) -> str:
    return self._server_address

  def insert(self, data, priorities: Dict[str, float]):
    """Inserts a "blob" (e.g. trajectory) into one or more priority tables.

    Note: The data is only stored once even if samples are inserted into
    multiple priority tables.

    Note: When possible, prefer to use the in graph version (see ./tf_client.py)
    to avoid stepping through Python.

    Args:
      data: A (possible nested) structure to insert.
      priorities: Mapping from table name to priority value.

    Raises:
      ValueError: If priorities is empty.
    """
    if not priorities:
      raise ValueError('priorities must contain at least one item')

    with self.writer(max_sequence_length=1) as writer:
      writer.append_timestep(data)
      for table, priority in priorities.items():
        writer.create_prioritized_item(
            table=table, num_timesteps=1, priority=priority)

  def writer(
      self,
      max_sequence_length: int,
      delta_encoded: bool = False,
      chunk_length: int = None,
  ) -> Writer:
    """Constructs a writer with a `max_sequence_length` buffer.

    The writer can be used to stream data of any length. `max_sequence_length`
    controls the size of the internal buffer and ensures that prioritized items
    can be created of any length <= `max_sequence_length`.

    The writer is stateful and must be closed after the write has finished. The
    easiest way to manage this is to use it as a contextmanager:

    ```python

        with client.writer(10) as writer:
           ...  # Write data of any length.

    ```

    If not used as a contextmanager then `.close()` must be called explicitly.

    Args:
      max_sequence_length: Size of the internal buffer controlling the upper
        limit of the number of timesteps which can be referenced in a single
        prioritized item. Note that this is NOT a limit of how many timesteps or
        items that can be inserted.
      delta_encoded: If `True` (False by default)  tensors are delta encoded
        against the first item within their respective batch before compressed.
        This can significantly reduce RAM at the cost of a small amount of CPU
        for highly correlated data (e.g frames of video observations).
      chunk_length: Number of timesteps grouped together before delta encoding
        and compression. Set by default to `min(10, max_sequence_length)` but
        can be overridden to achieve better compression rates when using longer
        sequences with a small overlap.

    Returns:
      A `Writer` with `max_sequence_length`.

    Raises:
      ValueError: If max_sequence_length < 1.
      ValueError: if chunk_length > max_sequence_length.
      ValueError: if chunk_length < 1.
    """
    if max_sequence_length < 1:
      raise ValueError('max_sequence_length (%d) must be a positive integer' %
                       max_sequence_length)

    if chunk_length is None:
      chunk_length = min(10, max_sequence_length)

    if chunk_length < 1 or chunk_length > max_sequence_length:
      raise ValueError(
          'chunk_length (%d) must be a positive integer le to max_sequence_length (%d)'
          % (chunk_length, max_sequence_length))

    return Writer(
        self._client.NewWriter(chunk_length, max_sequence_length,
                               delta_encoded))

  def sample(
      self,
      table: str,
      num_samples=1) -> Generator[List[replay_sample.ReplaySample], None, None]:
    """Samples `num_samples` items from table `table` of the Server.

    NOTE: This method should NOT be used for real training. TFClient (see
    tf_client.py) has far superior performance and should always be preferred.

    Note: If data was written using `insert` (e.g when inserting complete
    trajectories) then the returned "sequence" will be a list of length 1
    containing the trajectory as a single item.

    If `num_samples` is greater than the number of items in `table`, (or
    a rate limiter is used to control sampling), then the returned generator
    will block when an item past the sampling limit is requested.  It will
    unblock when sufficient additional items have been added to `table`.

    Example:
    ```python
    server = Server(..., tables=[queue("queue", ...)])
    client = Client(...)
    # Don't insert anything into "queue"
    generator = client.sample("queue")
    generator.next()  # Blocks until another thread/process writes to queue.
    ```

    Args:
      table: Name of the priority table to sample from.
      num_samples: (default to 1) The number of samples to fetch.

    Yields:
      Lists of timesteps (lists of instances of `ReplaySample`).
      If data was inserted into the table via `insert`, then each element
      of the generator is a length 1 list containing a `ReplaySample`.
      If data was inserted via a writer, then each element is a list whose
      length is the sampled trajectory's length.
    """
    sampler = self._client.NewSampler(table, num_samples, 1)

    for _ in range(num_samples):
      sequence = []
      last = False

      while not last:
        step, last = sampler.GetNextTimestep()
        key = int(step[0])
        probability = float(step[1])
        table_size = int(step[2])
        data = step[3:]
        sequence.append(
            replay_sample.ReplaySample(
                info=replay_sample.SampleInfo(key, probability, table_size),
                data=data))

      yield sequence

  def mutate_priorities(self,
                        table: str,
                        updates: Dict[int, float] = None,
                        deletes: List[int] = None):
    """Updates and/or deletes existing items in a priority table.

    NOTE: Whenever possible, prefer to use `TFClient.update_priorities`
    instead to avoid leaving the graph.

    Actions are executed in the same order as the arguments are specified.

    Args:
      table: Name of the priority table to update.
      updates: Mapping from priority item key to new priority value. If a key
        cannot be found then it is ignored.
      deletes: List of keys for priority items to delete. If a key cannot be
        found then it is ignored.
    """
    if updates is None:
      updates = {}
    if deletes is None:
      deletes = []
    self._client.MutatePriorities(table, list(updates.items()), deletes)

  def reset(self, table: str):
    """Clears all items of the table and resets its RateLimiter.

    Args:
      table: Name of the priority table to reset.
    """
    self._client.Reset(table)

  def server_info(
      self,
      timeout: Optional[int] = None) -> Dict[str, reverb_types.TableInfo]:
    """Get table metadata information.

    Args:
      timeout: Timeout in seconds to wait for server response. By default no
        deadline is set and call will block indefinetely until server responds.

    Returns:
      A dictionary mapping table names to their associated `TableInfo`
      instances, which contain metadata about the table.

    Raises:
      errors.TimeoutError: If timeout provided and exceeded.
    """
    try:
      info_proto_strings = self._client.ServerInfo(timeout or 0)
    except RuntimeError as e:
      if 'Deadline Exceeded' in str(e) and timeout is not None:
        raise errors.TimeoutError(
            f'ServerInfo call did not complete within provided timeout of '
            f'{timeout}s')
      raise

    table_info = {}
    for proto_string in info_proto_strings:
      proto = schema_pb2.TableInfo.FromString(proto_string)
      if proto.HasField('signature'):
        signature = nested_structure_coder.StructureCoder().decode_proto(
            proto.signature)
      else:
        signature = None
      info_dict = dict((descr.name, getattr(proto, descr.name))
                       for descr in proto.DESCRIPTOR.fields)
      info_dict['signature'] = signature
      name = str(info_dict['name'])
      table_info[name] = reverb_types.TableInfo(**info_dict)
    return table_info

  def checkpoint(self) -> str:
    """Triggers a checkpoint to be created.

    Returns:
      Absolute path to the saved checkpoint.
    """
    return self._client.Checkpoint()