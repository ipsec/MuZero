from abc import ABC
from typing import Callable, List

import tensorflow as tf
from tensorflow.keras.models import Model
from tensorflow.keras.regularizers import l2
from tensorflow.keras.layers import Dense

from config import MuZeroConfig
from games.game import Action
from models import NetworkOutput
from utils import tf_support_to_scalar


def scale(t: tf.Tensor):
    return (t - tf.reduce_min(t)) / (tf.reduce_max(t) - tf.reduce_min(t))


class Dynamics(Model, ABC):
    def __init__(self, hidden_state_size: int, encoded_space_size: int):
        """
        r^k, s^k = g_0(s^(k-1), a^k)
        :param encoded_space_size: size of hidden state
        """
        super(Dynamics, self).__init__()
        neurons = 20
        self.support = 300
        self.regularizer = l2(1e-4)
        self.inputs = Dense(neurons,
                            # input_shape=(encoded_space_size,),
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="g_input")
        self.hidden = Dense(neurons,
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="g_hidden")
        self.s_k = Dense(hidden_state_size,
                         activation=tf.nn.tanh,
                         # kernel_regularizer=self.regularizer,
                         name="g_s_k")

        self.r_k = Dense(self.support * 2 + 1,
                         activation=tf.nn.softmax,
                         # kernel_regularizer=self.regularizer,
                         name="g_r_k")

    # @tf.function
    def call(self, encoded_space, **kwargs):
        """
        :param **kwargs:
        :param encoded_space: hidden state concatenated with one_hot action
        :return: NetworkOutput with reward (r^k) and hidden state (s^k)
        """
        x = self.inputs(encoded_space)
        x = self.hidden(x)
        s_k = self.s_k(x)
        r_k = self.r_k(x)
        return s_k, r_k


class Prediction(Model, ABC):
    def __init__(self, action_state_size: int, hidden_state_size: int):
        """
        p^k, v^k = f_0(s^k)
        :param action_state_size: size of action state
        """
        super(Prediction, self).__init__()
        neurons = 20
        self.support = 300
        self.regularizer = l2(1e-4)
        self.inputs = Dense(neurons,
                            # input_shape=(hidden_state_size,),
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="f_inputs")
        self.hidden = Dense(neurons,
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="f_hidden")
        self.policy = Dense(action_state_size,
                            activation=tf.nn.softmax,
                            # kernel_regularizer=self.regularizer,
                            name="f_policy")
        self.value = Dense(self.support * 2 + 1,
                           activation=tf.nn.softmax,
                           # kernel_regularizer=self.regularizer,
                           name="f_value")

    # @tf.function
    def call(self, hidden_state, **kwargs):
        """
        :param hidden_state
        :return: NetworkOutput with policy logits and value
        """
        x = self.inputs(hidden_state)
        x = self.hidden(x)
        policy = self.policy(x)
        value = self.value(x)

        return policy, value


class Representation(Model, ABC):
    def __init__(self, observation_space_size: int):
        """
        s^0 = h_0(o_1,...,o_t)
        :param observation_space_size
        """
        super(Representation, self).__init__()
        neurons = 20
        self.regularizer = l2(1e-4)
        self.inputs = Dense(neurons,
                            # input_shape=(observation_space_size,),
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="h_inputs")
        self.hidden = Dense(neurons,
                            activation=tf.nn.relu,
                            # kernel_regularizer=self.regularizer,
                            name="h_hidden")
        self.s0 = Dense(observation_space_size,
                        activation=tf.nn.tanh,
                        # kernel_regularizer=self.regularizer,
                        name="h_s0")

    # @tf.function
    def call(self, observation, **kwargs):
        """
        :param observation
        :return: state s0
        """
        x = self.inputs(observation)
        x = self.hidden(x)
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
        s_0 = self.h_representation(observation)

        # prediction
        p, v = self.f_prediction(s_0)

        return NetworkOutput(
            value=tf_support_to_scalar(v, 300),
            reward=tf.convert_to_tensor([[0.0]]),
            policy_logits=NetworkOutput.build_policy_logits(policy_logits=p),
            hidden_state=s_0,
        )

    def recurrent_inference(self, hidden_state, action: Action) -> NetworkOutput:
        # dynamics + prediction function

        # dynamics (encoded_state)
        one_hot = tf.expand_dims(tf.one_hot(action.index, self.config.action_space_size), 0)
        encoded_state = tf.concat([hidden_state, one_hot], axis=1)
        s_k, r_k = self.g_dynamics(encoded_state)

        # prediction
        p, v = self.f_prediction(s_k)

        return NetworkOutput(
            value=tf_support_to_scalar(v, 300),
            reward=tf_support_to_scalar(r_k, 300),
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

    def training_steps_counter(self) -> int:
        return self._training_steps

    def increment_training_steps(self):
        self._training_steps += 1

    def get_variables_by_network(self):
        return [[x.trainable_variables] for x in [self.g_dynamics, self.f_prediction, self.h_representation]]

    def get_variables(self):
        return [x.trainable_variables for x in [self.g_dynamics, self.f_prediction, self.h_representation]]
