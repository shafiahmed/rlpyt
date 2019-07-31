
import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.nn.paralell import DistributedDataParallelCPU as DDPC

from rlpyt.agents.base import BaseAgent, AgentStep
from rlpyt.models.qpg.mlp import QofMuMlpModel, VMlpModel, PiMlpModel
from rlpyt.utils.quick_args import save__init__args
from rlpyt.distributions.gaussian import Gaussian, DistInfoStd
from rlpyt.utils.buffer import buffer_to
from rlpyt.utils.logging import logger
from rlpyt.models.utils import update_state_dict
from rlpyt.utils.collections import namedarraytuple


MIN_LOG_STD = -20
MAX_LOG_STD = 2

AgentInfo = namedarraytuple("AgentInfo", ["dist_info"])


class SacAgent(BaseAgent):

    shared_pi_model = None

    def __init__(
            self,
            ModelCls=PiMlpModel,  # Pi model.
            QModelCls=QofMuMlpModel,
            VModelCls=VMlpModel,
            model_kwargs=None,  # Pi model.
            q_model_kwargs=None,
            v_model_kwargs=None,
            initial_model_state_dict=None,  # Pi model.
            initial_q1_model_state_dict=None,
            initial_q2_model_state_dict=None,
            initial_v_model_state_dict=None,
            action_squash=1,  # Max magnitude (or None).
            pretrain_std=5.,  # High value to make near uniform sampling.
            ):
        if model_kwargs is None:
            model_kwargs = dict(hidden_sizes=[256, 256])
        if q_model_kwargs is None:
            q_model_kwargs = dict(hidden_sizes=[256, 256])
        if v_model_kwargs is None:
            v_model_kwargs = dict(hidden_sizes=[256, 256])
        save__init__args(locals())
        self.min_itr_learn = 0  # Get from algo.

    def initialize(self, env_spaces, share_memory=False,
            global_B=1, env_ranks=None):
        super().initialize(env_spaces, share_memory)
        self.q1_model = self.QModelCls(**self.env_model_kwargs, **self.q_model_kwargs)
        self.q2_model = self.QModelCls(**self.env_model_kwargs, **self.q_model_kwargs)
        self.v_model = self.VModelCls(**self.env_model_kwargs, **self.v_model_kwargs)
        if self.initial_q1_model_state_dict is not None:
            self.q1_model.load_state_dict(self.initial_q1_model_state_dict)
        if self.initial_q2_model_state_dict is not None:
            self.q2_model.load_state_dict(self.initial_q2_model_state_dict)
        if self.initial_v_model_state_dict is not None:
            self.v_model.load_state_dict(self.initial_v_model_state_dict)
        self.target_v_model = self.VModelCls(**self.env_model_kwargs,
            **self.v_model_kwargs)
        self.target_v_model.load_state_dict(self.v_model.state_dict())
        assert len(env_spaces.action.shape) == 1
        self.distribution = Gaussian(
            dim=env_spaces.action.shape[0],
            squash=self.action_squash,
            min_std=np.exp(MIN_LOG_STD),
            max_std=np.exp(MAX_LOG_STD),
        )

    def to_device(self, cuda_idx=None):
        super().to_device(cuda_idx)
        self.q1_model.to(self.device)
        self.q2_model.to(self.device)
        self.v_model.to(self.device)
        self.target_v_model.to(self.device)

    def data_parallel(self):
        super().data_parallel
        DDP_WRAP = DDPC if self.device.type == "cpu" else DDP
        self.q1_model = DDP_WRAP(self.q1_model)
        self.q2_model = DDP_WRAP(self.q2_model)
        self.v_model = DDP_WRAP(self.v_model)

    def give_min_itr_learn(self, min_itr_learn):
        self.min_itr_learn = min_itr_learn  # From algo.

    def make_env_to_model_kwargs(self, env_spaces):
        assert len(env_spaces.action.shape) == 1
        return dict(
            observation_shape=env_spaces.observation.shape,
            action_size=env_spaces.action.shape[0],
        )

    def q(self, observation, prev_action, prev_reward, action):
        model_inputs = buffer_to((observation, prev_action, prev_reward,
            action), device=self.device)
        q1 = self.q1_model(*model_inputs)
        q2 = self.q2_model(*model_inputs)
        return q1.cpu(), q2.cpu()

    def v(self, observation, prev_action, prev_reward):
        model_inputs = buffer_to((observation, prev_action, prev_reward),
            device=self.device)
        v = self.v_model(*model_inputs)
        return v.cpu()

    def pi(self, observation, prev_action, prev_reward):
        model_inputs = buffer_to((observation, prev_action, prev_reward),
            device=self.device)
        mean, log_std = self.pi_model(*model_inputs)
        dist_info = DistInfoStd(mean=mean, log_std=log_std)
        action, log_pi = self.distribution.sample_loglikelihood(dist_info)
        # action = self.distribution.sample(dist_info)
        # log_pi = self.distribution.log_likelihood(action, dist_info)
        log_pi, dist_info = buffer_to((log_pi, dist_info), device="cpu")
        return action, log_pi, dist_info  # Action stays on device for q models.

    def target_v(self, observation, prev_action, prev_reward):
        model_inputs = buffer_to((observation, prev_action, prev_reward),
            device=self.device)
        target_v = self.target_v_model(*model_inputs)
        return target_v.cpu()

    @torch.no_grad()
    def step(self, observation, prev_action, prev_reward):
        model_inputs = buffer_to((observation, prev_action, prev_reward),
            device=self.device)
        mean, log_std = self.pi_model(*model_inputs)
        dist_info = DistInfoStd(mean=mean, log_std=log_std)
        action = self.distribution.sample(dist_info)
        agent_info = AgentInfo(dist_info=dist_info)
        action, agent_info = buffer_to((action, agent_info), device="cpu")
        if np.any(np.isnan(action.numpy())):
            breakpoint()
        return AgentStep(action=action, agent_info=agent_info)

    def update_target(self, tau=1):
        update_state_dict(self.target_v_model, self.v_model, tau)

    @property
    def models(self):
        return self.q1_model, self.q2_model, self.v_model, self.pi_model

    def parameters(self):
        for model in self.models:
            yield from model.parameters()

    def parameters_by_model(self):
        return (model.parameters() for model in self.models)

    def train_mode(self, itr):
        for model in self.models:
            model.train()
        self._mode = "train"

    def sample_mode(self, itr):
        for model in self.models:
            model.eval()
        std = None if itr >= self.min_itr_learn else self.pretrain_std
        self.distribution.set_std(std)  # If None: std from policy dist_info.
        self._mode = "sample"

    def eval_mode(self, itr):
        for model in self.models:
            model.eval()
        self.distribution.set_std(0.)  # Deterministic (dist_info std ignored).
        self._mode = "eval"

    def state_dict(self):
        return dict(
            model=self.model.state_dict(),  # Pi model.
            q1_model=self.q1_model.state_dict(),
            q2_model=self.q2_model.state_dict(),
            v_model=self.v_model.state_dict(),
            target_v_model=self.target_v_model.state_dict(),
        )
