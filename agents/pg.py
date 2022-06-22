from torch import nn
import torch as t
import numpy as np
from typing import List, Any, Dict, Optional
from .utils import HeartsStateParser, Memory, Trajectory, StateParser, cumulative_rewards
from .training_helpers import Optimizers, Initializers, Activations
from . import Agent
from numpy.random._generator import Generator, default_rng

class REINFORCEAgent(nn.Module, Agent):
	def __init__(self, 
				 batch_size: int,
				 full_deck,
				 learning_rate,
				 gamma = 0.95,
				 importance_weighting = False,
				 queue_size = 2000,
				 layers: List[int] =[],
				 rng: Generator =default_rng(2137),
				 optimizer='adam',
				 optimizer_params: Dict[str, Any] = {},
				 activation='relu',
				 initializer='xavier_u',
				 initializer_params: Dict[str, Any] = {}):
	 
		nn.Module.__init__(self)
		Agent.__init__(self, full_deck, learning_rate, 0.0, gamma, rng)
		parser = HeartsStateParser(full_deck)
		self.batch_size = batch_size
		state_size = parser.state_len
		self.losses = []
		self.parser = parser
		self.state_size = state_size
		self.action_size = 13 * 4 if full_deck else 6 * 4
		self.rollouts = Memory[Trajectory](None, Trajectory)
		self.memory = Memory[Trajectory](queue_size, Trajectory) if importance_weighting else None 
		self.last_prob: float = 0.0
		self.importance_weighting = importance_weighting
  
		layer_init = lambda _in, _out, activation=None: nn.Sequential(
			nn.Linear(_in, _out), Activations.get(activation)
		)
  
		activations = [activation] * (len(layers))
		activations.append('')
		print(state_size, self.action_size)
		layers: List[int] = [state_size] + layers
		layers.append(self.action_size)
  
		self.qnet = nn.Sequential(*[
	 		layer_init(_in, _out, _activation) for _in, _out, _activation in zip(layers[:-1], layers[1:], activations)
		])
  
		for module in self.modules():
			if isinstance(module, nn.Linear): 
				Initializers.get(initializer)(module.weight, **initializer_params)
				Initializers.get('const')(module.bias, val=0)
	
		optimizer_params.update(lr=self.alpha)
  
		self.optimizer = Optimizers.get(optimizer)(self.qnet.parameters(), **optimizer_params)
  
		self.learning_device = "cuda" if t.cuda.is_available() else 'cpu'
		self.eval_device = 'cpu'
		self = self.to(self.learning_device)
  
	def set_temp_reward(self, discarded_cards: dict, point_deltas: dict):	
		super().set_temp_reward(discarded_cards, point_deltas)
		self.remember(self.parser.parse(self.previous_state), self.previous_action, -self.current_reward)
	
	def set_final_reward(self, points: dict):
		super().set_final_reward(points)
		# TODO sth with points in total.
  
		self.losses.append(self.replay())
	
	def remember(self, state, action, reward):
		if not isinstance(state, t.Tensor): state= self.parser.parse(state)
		#Function adds information to the memory about last action and its results
		self.rollouts.store(Trajectory(state, action, reward, self.last_prob))
	
	def forward(self, state):
		return self.qnet(state.to(self.learning_device))

	def get_name(self) -> str:
		return super().get_name() + " - REINFORCE"
 
	def get_action(self, state, invalid_actions: Optional[List[int]] = None):
		"""
		Compute the action to take in the current state, basing on policy returned by the network.

		Note: To pick action according to the probability generated by the network
		"""

		#
		# INSERT CODE HERE to get action in a given state
		# 
		state: t.Tensor =self.parser.parse(state)
		possible_actions = set(range(0, self.action_size))
  
		if invalid_actions:
			possible_actions -= set(invalid_actions)

		with t.no_grad():
			self.eval()
			logits: t.Tensor = self(state).cpu().squeeze(0)
			probs: t.Tensor = t.softmax(logits, dim=0)
		possible_actions = list(possible_actions)
		probs_gathered: np.ndarray = probs.gather(0, t.as_tensor((possible_actions))).numpy().flatten() +1e-8
		probs_gathered = probs_gathered/probs_gathered.sum()
		action = self.rng.choice(possible_actions, p=probs_gathered)
		self.last_prob = probs[action].item()
		
		return action

	def get_best_action(self, state, invalid_actions: Optional[List[int]] = None):
		state: t.Tensor =self.parser.parse(state)
		possible_actions = set(range(0, self.action_size))
  
		if invalid_actions:
			possible_actions -= set(invalid_actions)

		possible_actions = list(possible_actions)

		with t.no_grad():
			self.eval()
			logits: t.Tensor = self(state).cpu().squeeze(0).gather(0, t.as_tensor(possible_actions))
			
			m, _ = logits.max(-1)
			indices: np.ndarray = t.nonzero(logits == m).numpy().flatten()
		
		cond = indices.size == 1
		action = (possible_actions[indices[0]] if cond else possible_actions[self.rng.choice(indices)])

		return action

	def replay(self):
		"""
		Function learn network using data stored in state, action and reward memory. 
		First calculates G_t for each state and train network
		"""
		#
		# INSERT CODE HERE to train network
		#
		
		batch_size = self.batch_size
		trajectories = self.rollouts.get(list(range(len(self.rollouts))))
		rewards = cumulative_rewards(self.gamma, trajectories.reward)
		trajectories = Trajectory(trajectories.state, trajectories.action,  tuple(rewards), trajectories.prob)
		self.rollouts.set_items(list(zip(*trajectories)))
		if self.importance_weighting:
			for tup in zip(*trajectories):
				self.memory.store(tup)
		mem = self.memory if self.importance_weighting else self.rollouts
		count_mem = len(mem)
		if count_mem < batch_size: return None
		
		batch = mem.sample(count_mem)
		print(batch.state[0].__len__())	
		states = t.stack(batch.state).to(self.learning_device)
		actions = t.as_tensor(batch.action, dtype=t.int64, device=self.learning_device).unsqueeze(1)
		rewards = t.as_tensor(batch.reward, device=self.learning_device).unsqueeze(1)
		probs = t.as_tensor(batch.prob, device=self.learning_device).unsqueeze(1)
   
		with t.no_grad():
			self.eval()
			current_policy: t.Tensor = t.softmax(self(states), dim=1)
			current_policy_a = current_policy.gather(1, actions)
			importance_weight = (current_policy_a + 1e-8) / (probs + 1e-8)
   
		self.train()
		predicted: t.Tensor = t.log_softmax(self(states), dim=1).gather(1, actions)
		
		loss = -predicted * importance_weight * rewards
		loss = t.sum(loss)
		self.optimizer.zero_grad()
		loss.backward()
		self.optimizer.step()
		self.rollouts.clear()
		return loss.cpu().item()
