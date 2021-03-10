from abc import ABC
from typing import Callable, List

import tensorflow as tf
from tensorflow.keras.layers import Dense
from tensorflow.keras.models import Model
from tensorflow.keras.initializers import Zeros, RandomUniform

from config import MuZeroConfig
from games.game import Action
from models import NetworkOutput

"""
g = dynamics
    inputs: hidden state (s^[k-1], a^k)
    outputs: intermediate reward (r^k), new hidden state (s^k) 
    
f = prediction
    inputs: hidden state (s^k)
    outputs: policy (p^k), value (v^k)

h = representation
    inputs: observation (o_n)
    outputs: hidden state (s^0)
"""


def scale(t: tf.Tensor):
    return (t - tf.reduce_min(t)) / (tf.reduce_max(t) - tf.reduce_min(t))


def support_to_scalar(logits: tf.Tensor, support_size: int = 20, eps: float = 0.001):
    """
    Transform a categorical representation to a scalar
    See paper appendix Network Architecture
    """
    # Decode to a scalar
    probabilities = tf.nn.softmax(logits, axis=1)
    support = tf.expand_dims(tf.range(-support_size, support_size + 1), axis=0)
    support = tf.tile(support, [logits.shape[0], 1])  # make batchsize supports
    # Expectation under softmax
    x = tf.cast(support, tf.float32) * probabilities
    x = tf.reduce_sum(x, axis=-1)
    # Inverse transform h^-1(x) from Lemma A.2.
    # From "Observe and Look Further: Achieving Consistent Performance on Atari" - Pohlen et al.
    x = tf.math.sign(x) * (((tf.math.sqrt(1. + 4. * eps * (tf.math.abs(x) + 1 + eps)) - 1) / (2 * eps)) ** 2 - 1)
    x = tf.expand_dims(x, 1)
    return x


def scalar_to_support(x: tf.Tensor, support_size: int = 20):
    x = tf.math.sign(x) * (tf.math.sqrt(tf.math.abs(x) + 1) - 1) + 0.001 * x
    x = tf.clip_by_value(x, -support_size, support_size)
    floor = tf.floor(x)
    prob = x - floor
    # logits = tf.zeros((x.shape[0], x.shape[1], 2 * support_size + 1))
    indices = tf.cast(tf.squeeze(floor + support_size), dtype=tf.int32)
    indices = tf.stack([tf.range(x.shape[1]), indices], axis=1)
    updates = tf.squeeze(1 - prob)

    indexes = floor + support_size + 1
    prob = tf.where(2 * support_size < indexes, 0.0, prob)
    indexes = tf.where(2 * support_size < indexes, 0.0, indexes)
    indexes = tf.squeeze(tf.cast(indexes, dtype=tf.int32))
    indexes = tf.stack([tf.range(x.shape[1]), indexes], axis=1)

    idx = tf.concat([indices, indexes], axis=0)
    prob = tf.squeeze(prob)
    all_updates = tf.concat([updates, prob], axis=0)

    return tf.scatter_nd(idx, all_updates, (x.shape[1], 2 * support_size + 1))


class Dynamics(Model, ABC):
    def __init__(self, hidden_state_size: int, encoded_space_size: int):
        """
        r^k, s^k = g_0(s^(k-1), a^k)
        :param encoded_space_size: size of hidden state
        """
        super(Dynamics, self).__init__()
        neurons = 32
        self.inputs = Dense(neurons, input_shape=(encoded_space_size,), activation=tf.nn.relu)
        self.hidden = Dense(neurons, activation=tf.nn.relu)
        self.common = Dense(neurons, activation=tf.nn.relu)
        self.s_k = Dense(hidden_state_size, activation=tf.nn.relu)
        self.r_k = Dense(41, kernel_initializer=Zeros(), activation=tf.nn.tanh)

    @tf.function
    def call(self, encoded_space, **kwargs):
        """
        :param encoded_space: hidden state concatenated with one_hot action
        :return: NetworkOutput with reward (r^k) and hidden state (s^k)
        """
        x = self.inputs(encoded_space)
        x = self.hidden(x)
        x = self.common(x)
        s_k = self.s_k(x)
        r_k = self.r_k(x)
        return s_k, support_to_scalar(r_k)


class Prediction(Model, ABC):
    def __init__(self, action_state_size: int, hidden_state_size: int):
        """
        p^k, v^k = f_0(s^k)
        :param action_state_size: size of action state
        """
        super(Prediction, self).__init__()
        neurons = 32
        self.inputs = Dense(neurons, input_shape=(hidden_state_size,), activation=tf.nn.relu)
        self.hidden = Dense(neurons, activation=tf.nn.relu)
        self.common = Dense(neurons, activation=tf.nn.relu)
        self.policy = Dense(action_state_size, activation=tf.nn.tanh, kernel_initializer='uniform')
        self.value = Dense(41, kernel_initializer=Zeros(), activation=tf.nn.tanh)

    @tf.function
    def call(self, hidden_state, **kwargs):
        """
        :param hidden_state
        :return: NetworkOutput with policy logits and value
        """
        x = self.inputs(hidden_state)
        x = self.hidden(x)
        x = self.common(x)
        policy = self.policy(x)
        value = self.value(x)

        return policy, support_to_scalar(value)


class Representation(Model, ABC):
    def __init__(self, observation_space_size: int):
        """
        s^0 = h_0(o_1,...,o_t)
        :param observation_space_size
        """
        super(Representation, self).__init__()
        neurons = 32
        self.inputs = Dense(neurons, input_shape=(observation_space_size,), activation=tf.nn.relu)
        self.hidden = Dense(neurons, activation=tf.nn.relu)
        self.common = Dense(neurons, activation=tf.nn.relu)
        self.s0 = Dense(observation_space_size, activation=tf.nn.relu)

    @tf.function
    def call(self, observation, **kwargs):
        """
        :param observation
        :return: state s0
        """
        x = self.inputs(observation)
        x = self.hidden(x)
        x = self.common(x)
        s_0 = self.s0(x)
        return s_0


class Network(object):
    def __init__(self, config: MuZeroConfig):
        self.config = config
        self.g_dynamics = Dynamics(config.state_space_size, config.action_space_size + config.state_space_size)
        self.f_prediction = Prediction(config.action_space_size, config.state_space_size)
        self.h_representation = Representation(config.state_space_size)
        self._training_steps = 0

    def initial_inference(self, observation) -> NetworkOutput:
        # representation + prediction function

        # representation
        observation = tf.expand_dims(observation, 0)
        # observation = scale(observation)      # Scale only hidden states or observations too?
        s_0 = self.h_representation(observation)

        # prediction
        p, v = self.f_prediction(s_0)

        return NetworkOutput(
            value=v,
            reward=0.0,
            policy_logits=NetworkOutput.build_policy_logits(policy_logits=p),
            hidden_state=s_0,
        )

    def recurrent_inference(self, hidden_state, action: Action) -> NetworkOutput:
        # dynamics + prediction function

        # dynamics (encoded_state)
        one_hot = tf.expand_dims(tf.one_hot(action.index, self.config.action_space_size), 0)
        hidden_state = scale(hidden_state)
        encoded_state = tf.concat([hidden_state, one_hot], axis=1)
        s_k, r_k = self.g_dynamics(encoded_state)

        # prediction
        p, v = self.f_prediction(s_k)

        return NetworkOutput(
            value=v,
            reward=r_k,
            policy_logits=NetworkOutput.build_policy_logits(policy_logits=p),
            hidden_state=s_k
        )

    def get_weights(self) -> List:
        networks = [self.g_dynamics, self.f_prediction, self.h_representation]
        return [variables
                for variables_list in map(lambda n: n.weights, networks)
                for variables in variables_list]

    def cb_get_variables(self) -> Callable:
        """Return a callback that return the trainable variables of the network."""

        def get_variables():
            networks = [self.g_dynamics, self.f_prediction, self.h_representation]
            return [variables
                    for variables_list in map(lambda n: n.weights, networks)
                    for variables in variables_list]

        return get_variables

    def get_networks(self) -> List:
        return [self.g_dynamics, self.f_prediction, self.h_representation]

    def training_steps(self) -> int:
        # How many steps / batches the network has been trained for.
        return int(self._training_steps / self.config.batch_size)

    def increment_training_steps(self):
        self._training_steps += 1

    def get_variables(self):
        return [x.trainable_variables for x in [self.g_dynamics, self.f_prediction, self.h_representation]]
