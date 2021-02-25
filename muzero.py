# Lint as: python3
"""Pseudocode description of the MuZero algorithm."""
# pylint: disable=unused-argument
# pylint: disable=missing-docstring
# pylint: disable=g-explicit-length-test
from pathlib import Path
from typing import Any

import numpy as np
import tensorflow as tf
from tensorflow.keras.optimizers import Adam
# MuZero training is split into two independent parts: Network training and
# self-play data generation.
# These two parts only communicate by transferring the latest network checkpoint
# from the training to the self-play, and the finished games from the self-play
# to the training.
from tqdm import trange

from config import MuZeroConfig
from games.game import ReplayBuffer, Game, make_atari_config
from mcts import Node, expand_node, backpropagate, add_exploration_noise, run_mcts, select_action
from models.network import Network
from storage import SharedStorage
from summary import write_summary
from utils import MinMaxStats


##################################
####### Part 1: Self-Play ########


# Each self-play job is independent of all others; it takes the latest network
# snapshot, produces a game and makes it available to the training job by
# writing it to a shared replay buffer.
def run_selfplay(config: MuZeroConfig, storage: SharedStorage, replay_buffer: ReplayBuffer):
    # while True:
    network = storage.latest_network()
    rewards = []

    for _ in range(config.num_games):
        game = play_game(config, network)
        rewards.append(np.sum(game.rewards))
        replay_buffer.save_game(game)

    return np.mean(rewards)



# Each game is produced by starting at the initial board position, then
# repeatedly executing a Monte Carlo Tree Search to generate moves until the end
# of the game is reached.
def play_game(config: MuZeroConfig, network: Network) -> Game:
    game = Game(config.action_space_size, config.discount)

    while not game.terminal() and len(game.history) < config.max_moves:
        min_max_stats = MinMaxStats(config.known_bounds)

        # At the root of the search tree we use the representation function to
        # obtain a hidden state given the current observation.
        root = Node(0)
        current_observation = game.make_image(-1)
        network_output = network.initial_inference(current_observation)
        expand_node(root, game.to_play(), game.legal_actions(), network_output)
        backpropagate([root], network_output.value, game.to_play(), config.discount, min_max_stats)
        add_exploration_noise(config, root)

        # We then run a Monte Carlo Tree Search using only action sequences and the
        # model learned by the network.
        run_mcts(config, root, game.action_history(), network, min_max_stats)
        action = select_action(config, len(game.history), root, network)
        game.apply(action)
        game.store_search_statistics(root)
    return game


######### End Self-Play ##########
##################################

##################################
####### Part 2: Training #########


def train_network(config: MuZeroConfig, storage: SharedStorage, replay_buffer: ReplayBuffer):
    network = storage.latest_network()
    optimizer = Adam(learning_rate=0.001)

    for _ in range(config.training_steps):
        batch = replay_buffer.sample_batch(config.num_unroll_steps, config.td_steps)
        update_weights(optimizer, network, batch, config.weight_decay)
        storage.save_network(network)

    storage.save_network(network)


def scale_gradient(tensor: Any, scale):
    """Scales the gradient for the backward pass."""
    return tensor * scale + tf.stop_gradient(tensor) * (1 - scale)


def update_weights(optimizer: tf.keras.optimizers.Optimizer, network: Network, batch, weight_decay: float):
    def loss():
        loss = 0

        for observations, actions, targets in batch:
            # Initial step, from the real observation.
            network_output = network.initial_inference(observations)
            hidden_state = network_output.hidden_state
            predictions = [(1.0, network_output)]

            # Recurrent steps, from action and previous hidden state.
            for action in actions:
                network_output = network.recurrent_inference(hidden_state, action)
                hidden_state = network_output.hidden_state
                predictions.append((1.0 / len(actions), network_output))

                hidden_state = scale_gradient(hidden_state, 0.5)

            for k, (prediction, target) in enumerate(zip(predictions, targets)):
                gradient_scale, network_output = prediction
                target_value, target_reward, target_policy = target

                if target_policy:
                    l = tf.nn.softmax_cross_entropy_with_logits(
                        logits=list(network_output.policy_logits.values()), labels=target_policy)
                    l += scalar_loss([network_output.value], [target_value])
                    if k > 0:
                        l += scalar_loss([network_output.reward], [target_reward])

                    loss += scale_gradient(l, gradient_scale)

        loss /= len(batch)

        for weights in network.get_weights():
            loss += weight_decay * tf.nn.l2_loss(weights)

        return loss

    optimizer.minimize(loss, var_list=network.cb_get_variables())
    network.increment_training_steps()


def scalar_loss(prediction, target):
    return tf.reduce_sum(tf.nn.softmax_cross_entropy_with_logits(labels=target, logits=prediction))


######### End Training ###########
##################################

################################################################################
############################# End of pseudocode ################################
################################################################################


def muzero(config: MuZeroConfig):
    storage = SharedStorage(config)

    with trange(10000) as t:
        for i in range(10000):
            replay_buffer = ReplayBuffer(config)
            score = run_selfplay(config, storage, replay_buffer)
            write_summary(i, score)
            train_network(config, storage, replay_buffer)
            save_checkpoints(storage.latest_network())
            t.set_description(f"Score: {score:.2f}")
            t.update(1)
            t.refresh()

    export_models(storage.latest_network())


def save_checkpoints(network: Network):
    try:
        for model in network.get_networks():
            Path.mkdir(Path(f'./checkpoints/{model.__class__.__name__}'), parents=True, exist_ok=True)
            model.save_weights(f'./checkpoints/{model.__class__.__name__}/checkpoint')
            # print(f"Model {model.__class__.__name__} Saved!")
    except Exception as e:
        print(f"Unable to save networks. {e}")


def load_checkpoints(network: Network):
    try:
        for model in network.get_networks():
            path = Path(f'./checkpoints/{model.__class__.__name__}/checkpoint')
            if Path.exists(path):
                model.load_weights(path)
                print(f"Load weights with success.")
    except Exception as e:
        print(f"Unable to load networks. {e}")


def export_models(network: Network):
    try:
        for model in network.get_networks():
            path = Path(f'./data/saved_model/{model.__class__.__name__}')
            Path.mkdir(path, parents=True, exist_ok=True)
            tf.saved_model.save(model, path)
            print(f"Model {model.__class__.__name__} Saved!")
    except Exception as e:
        print(f"Unable to save networks. {e}")


if __name__ == "__main__":
    config = make_atari_config()
    muzero(config)
