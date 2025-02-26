from typing import Optional

import torch

from tensordict.tensordict import TensorDict
from torchrl.data import (
    BoundedTensorSpec,
    CompositeSpec,
    UnboundedContinuousTensorSpec,
    UnboundedDiscreteTensorSpec,
)

from rl4co.envs.base import RL4COEnvBase

MAX_LENGTHS = {20: 2.0, 50: 3.0, 100: 4.0}


class OPEnv(RL4COEnvBase):
    """Orienteering Problem (OP) environment
    At each step, the agent chooses a city to visit. The reward is the -infinite unless the agent visits all the cities.

    Args:
        - num_loc <int>: number of locations (cities) in the VRP. NOTE: the depot is included
        - min_loc <float>: minimum value for the location coordinates
        - max_loc <float>: maximum value for the location coordinates
        - length_capacity <float>: capacity of the vehicle of length, i.e. the maximum length the vehicle can travel
        - td_params <TensorDict>: parameters of the environment
        - seed <int>: seed for the environment
        - device <str>: 'cpu' or 'cuda:0', device to use.  Generally, no need to set as tensors are updated on the fly
    Note:
        - in our setting, the vehicle has to come back to the depot at the end
    """

    name = "op"

    def __init__(
        self,
        num_loc: int = 10,
        min_loc: float = 0,
        max_loc: float = 1,
        min_prize: float = 0.01,
        max_prize: float = 1.01,
        td_params: TensorDict = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.num_loc = num_loc
        self.min_loc = min_loc
        self.max_loc = max_loc
        self.min_prize = min_prize
        self.max_prize = max_prize
        self.length_capacity = MAX_LENGTHS[num_loc]
        self._make_spec(td_params)

    def _step(self, td: TensorDict) -> TensorDict:
        """Update the states of the environment
        Args:
            - td <TensorDict>: tensor dictionary containing with the action
                - action <int> [batch_size, 1]: action to take
        NOTE:
            - the first node in de prize is larger than 0 or less than 0?
            - this design is important. For now the design is LESS than 0
        """
        current_node = td["action"][..., None]
        current_node_expand = current_node[..., None].repeat_interleave(2, dim=-1)
        length_capacity = td["length_capacity"]
        prize = td["prize"]
        prize_collect = td["prize_collect"]

        # Collect prize
        prize_collect += torch.gather(prize, -1, current_node)

        # Set the visited node prize to -1
        prize.scatter_(-1, current_node, 0)

        # Update the used length capacity
        length_capacity -= (
            torch.gather(td["locs"], -2, current_node_expand)
            - torch.gather(
                td["locs"],
                -2,
                td["current_node"][..., None].repeat_interleave(2, dim=-1),
            )
        ).norm(p=2, dim=-1)

        # Get the action mask, no zero prize nodes can be visited
        action_mask = prize > 0

        # Nodes distance exceeding length capacity cannot be visited
        length_to_next_node = (
            td["locs"] - torch.gather(td["locs"], -2, current_node_expand)
        ).norm(p=2, dim=-1)
        length_to_next_node_and_return = length_to_next_node + td["length_to_depot"]
        action_mask = torch.logical_and(
            action_mask, length_to_next_node_and_return <= length_capacity
        )

        # We are done if run out the lenght capacity, i.e. no available node to visit
        done = torch.count_nonzero(action_mask.float(), dim=-1) <= 1e-5

        # If done, then set the depot be always available
        action_mask[..., 0] = torch.logical_or(action_mask[..., 0], done)

        # Calculate reward (minus length of path, since we want to maximize the reward -> minimize the path length)
        # Note: reward is calculated outside for now via the get_reward function
        # to calculate here need to pass action sequence or save it as state
        reward = torch.ones_like(done) * float("-inf")

        # The output must be written in a ``"next"`` entry
        return TensorDict(
            {
                "next": {
                    "locs": td["locs"],
                    "length_capacity": td["length_capacity"],
                    "length_to_depot": td["length_to_depot"],
                    "current_node": current_node,
                    "prize": prize,
                    "prize_collect": prize_collect,
                    "action_mask": action_mask,
                    "reward": reward,
                    "done": done,
                }
            },
            td.shape,
        )

    def _reset(self, td: Optional[TensorDict] = None, batch_size=None) -> TensorDict:
        """
        Args:
            - td (Optional) <TensorDict>: tensor dictionary containing the initial state
        """
        if batch_size is None:
            batch_size = self.batch_size if td is None else td["prize"].shape[:-1]
        if td is None or td.is_empty():
            td = self.generate_data(batch_size=batch_size)
        device = td.device

        observation = torch.cat([td["depot"][..., None, :], td["locs"]], dim=-2)
        prize_depot = torch.zeros((*batch_size, 1), device=device)
        prize = torch.cat([prize_depot, td["prize"]], dim=-1)

        # Initialize the current node
        current_node = torch.zeros((*batch_size, 1), dtype=torch.int64, device=device)

        # Initialize the capacity
        length_capacity = torch.full(
            (*batch_size, 1), self.length_capacity, dtype=torch.float32, device=device
        )

        # Calculate the lenght of each node back to the depot
        length_to_depot = (observation - td["depot"][..., None, :]).norm(p=2, dim=-1)

        # Init the action mask
        action_mask = prize > 1e-5

        # Calculate the distance of each node at this moment
        current_node_loccation = torch.gather(
            observation, -2, current_node[..., None].repeat_interleave(2, dim=-1)
        )
        length_to_next_node = (observation - current_node_loccation).norm(p=2, dim=-1)
        length_to_next_node_and_return = length_to_next_node + length_to_depot
        action_mask = torch.logical_and(
            action_mask, length_to_next_node_and_return <= length_capacity
        )

        return TensorDict(
            {
                "locs": observation,
                "length_capacity": length_capacity,
                "length_to_depot": length_to_depot,
                "current_node": current_node,
                "prize": prize,
                "prize_collect": torch.zeros_like(length_capacity),
                "action_mask": action_mask,
            },
            batch_size=batch_size,
        )

    def _make_spec(self, td_params: TensorDict = None):
        """Make the observation and action specs from the parameters."""
        self.observation_spec = CompositeSpec(
            locs=BoundedTensorSpec(
                minimum=self.min_loc,
                maximum=self.max_loc,
                shape=(self.num_loc, 2),
                dtype=torch.float32,
            ),
            length_capacity=BoundedTensorSpec(
                minimum=0,
                maximum=self.length_capacity,
                shape=(1),
                dtype=torch.float32,
            ),
            length_to_depot=BoundedTensorSpec(
                minimum=0,
                maximum=self.length_capacity,
                shape=(1),
                dtype=torch.float32,
            ),
            current_node=UnboundedDiscreteTensorSpec(
                shape=(1),
                dtype=torch.int64,
            ),
            prize=BoundedTensorSpec(
                minimum=-1,
                maximum=self.max_prize,
                shape=(self.num_loc),
                dtype=torch.float32,
            ),
            prize_collect=UnboundedContinuousTensorSpec(
                shape=(self.num_loc),
                dtype=torch.float32,
            ),
            action_mask=UnboundedDiscreteTensorSpec(
                shape=(self.num_loc),
                dtype=torch.bool,
            ),
            shape=(),
        )
        self.input_spec = self.observation_spec.clone()
        self.action_spec = BoundedTensorSpec(
            shape=(1,),
            dtype=torch.int64,
            minimum=0,
            maximum=self.num_loc,
        )
        self.reward_spec = UnboundedContinuousTensorSpec()
        self.done_spec = UnboundedDiscreteTensorSpec(dtype=torch.bool)

    def get_reward(self, td, actions) -> TensorDict:
        """Function to compute the reward. Can be called by the agent to compute the reward of the current state
        This is faster than calling step() and getting the reward from the returned TensorDict at each time for CO tasks
        """
        # Check that tours are valid, i.e. contain 0 to n -1
        sorted_actions = actions.data.sort(1)[0]
        # Make sure each node visited once at most (except for depot)
        assert (
            (sorted_actions[:, 1:] == 0)
            | (sorted_actions[:, 1:] > sorted_actions[:, :-1])
        ).all(), "Duplicates"

        d = td["locs"].gather(
            1, actions[..., None].expand(*actions.size(), td["locs"].size(-1))
        )
        length = (
            (d[:, 1:] - d[:, :-1]).norm(p=2, dim=-1).sum(1)  # Prevent error if len 1 seq
            + (d[:, 0] - td["locs"][..., 0, :]).norm(p=2, dim=-1)  # Depot to first
            + (d[:, -1] - td["locs"][..., 0, :]).norm(
                p=2, dim=-1
            )  # Last to depot, will be 0 if depot is last
        )
        assert (
            length <= self.length_capacity + 1e-5
        ).all(), "Max length exceeded by {}".format(
            (length - td["length_capacity"]).max()
        )

        return td["prize_collect"].squeeze(-1)

    def generate_data(self, batch_size):
        """
        Args:
            - batch_size <int> or <list>: batch size
        Returns:
            - td <TensorDict>: tensor dictionary containing the initial state
                - locs <Tensor> [batch_size, num_loc, 2]: locations of the nodes
                - prize <Tensor> [batch_size, num_loc]: prize of the nodes
                - capacity <Tensor> [batch_size, 1]: capacity of the vehicle
                - current_node <Tensor> [batch_size, 1]: current node
                - i <Tensor> [batch_size, 1]: number of visited nodes
        NOTE:
            - the locs includes the depot as the first node
            - the prize includes the used capacity at the first value
            - the unvisited variable can be replaced by prize > 0
        """
        # Batch size input check
        batch_size = [batch_size] if isinstance(batch_size, int) else batch_size

        # Initialize the locations (including the depot which is always the first node)
        locs = (
            torch.FloatTensor(*batch_size, self.num_loc + 1, 2)
            .uniform_(self.min_loc, self.max_loc)
            .to(self.device)
        )

        # Initialize the prize
        prize_ = (locs[..., :1, :] - locs[..., 1:, :]).norm(p=2, dim=-1)
        prize = (
            1 + (prize_ / prize_.max(dim=-1, keepdim=True)[0] * 99).int()
        ).float() / 100.0

        return TensorDict(
            {
                "locs": locs[..., 1:, :],
                "depot": locs[..., 0, :],
                "prize": prize,
            },
            batch_size=batch_size,
        )

    def render(self, td):
        # TODO
        """Render the environment"""
        raise NotImplementedError
