# Copyright 2020 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MNIST example.

This script trains a simple Convolutional Neural Net on the MNIST dataset.
The data is loaded using tensorflow_datasets.

"""
from absl import app
from absl import flags
from absl import logging

from flax import jax_utils
from flax import nn
from flax import optim

import jax
from jax import random

import jax.numpy as jnp

import numpy as onp

import tensorflow_datasets as tfds


FLAGS = flags.FLAGS

flags.DEFINE_float(
    'learning_rate', default=0.1,
    help=('The learning rate for the momentum optimizer.'))

flags.DEFINE_float(
    'momentum', default=0.9,
    help=('The decay rate used for the momentum optimizer.'))

flags.DEFINE_integer(
    'batch_size', default=128,
    help=('Batch size for training.'))

flags.DEFINE_integer(
    'num_epochs', default=10,
    help=('Number of training epochs.'))


def load_split(split):
  ds = tfds.load('mnist', split=split, batch_size=-1)
  data = tfds.as_numpy(ds)
  data['image'] = onp.float32(data['image']) / 255.
  return data


class CNN(nn.Module):
  """A simple CNN model."""

  def apply(self, x):
    x = nn.Conv(x, features=32, kernel_size=(3, 3))
    x = nn.relu(x)
    x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
    x = nn.Conv(x, features=64, kernel_size=(3, 3))
    x = nn.relu(x)
    x = nn.avg_pool(x, window_shape=(2, 2), strides=(2, 2))
    x = x.reshape((x.shape[0], -1))  # flatten
    x = nn.Dense(x, features=256)
    x = nn.relu(x)
    x = nn.Dense(x, features=10)
    x = nn.log_softmax(x)
    return x


def create_model(key):
  _, initial_params = CNN.init_by_shape(key, [((1, 28, 28, 1), jnp.float32)])
  model = nn.Model(CNN, initial_params)
  return model


@jax.pmap
def create_optimizers(rng):
  optimizer_def = optim.Momentum(
      learning_rate=FLAGS.learning_rate, beta=FLAGS.momentum)
  optimizer = optimizer_def.create(create_model(rng))
  return optimizer


def onehot(labels, num_classes=10):
  x = (labels[..., None] == jnp.arange(num_classes)[None])
  return x.astype(jnp.float32)


def cross_entropy_loss(logits, labels):
  return -jnp.mean(jnp.sum(onehot(labels) * logits, axis=-1))


def compute_metrics(logits, labels):
  loss = cross_entropy_loss(logits, labels)
  accuracy = jnp.mean(jnp.argmax(logits, -1) == labels)
  metrics = {
      'loss': loss,
      'accuracy': accuracy,
  }
  return metrics


@jax.pmap
def train_step(optimizer, batch):
  """Train for a single step."""
  def loss_fn(model):
    logits = model(batch['image'])
    loss = cross_entropy_loss(logits, batch['label'])
    return loss, logits
  grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
  (_, logits), grad = grad_fn(optimizer.target)
  optimizer = optimizer.apply_gradient(grad)
  metrics = compute_metrics(logits, batch['label'])
  return optimizer, metrics


@jax.pmap
def eval_step(model, batch):
  logits = model(batch['image'])
  return compute_metrics(logits, batch['label'])


def train_epoch(optimizers, train_ds, batch_size, epoch, rng):
  """Train for a single epoch."""
  train_ds_size = len(train_ds['image'])
  steps_per_epoch = train_ds_size // batch_size

  perms = rng.permutation(len(train_ds['image']))
  perms = perms[:steps_per_epoch * batch_size]  # skip incomplete batch
  perms = perms.reshape((steps_per_epoch, batch_size))
  batch_metrics = []
  for perm in perms:
    batch = {k: v[perm] for k, v in train_ds.items()}
    batch = jax_utils.replicate(batch)
    optimizers, metrics = train_step(optimizers, batch)
    batch_metrics.append(metrics)

  # compute mean of metrics across each batch in epoch.
  batch_metrics_np = jax.device_get(batch_metrics)
  batch_metrics_np = jax.tree_multimap(lambda *xs: onp.array(xs),
                                       *batch_metrics_np)
  epoch_metrics_np = {
      k: onp.mean(batch_metrics_np[k], axis=0) for k in batch_metrics_np
  }
  logging.info('train epoch: %d, loss: %s, accuracy: %s', epoch,
               epoch_metrics_np['loss'], epoch_metrics_np['accuracy'] * 100)

  return optimizers, epoch_metrics_np


def eval_model(models, test_ds):
  metrics = eval_step(models, test_ds)
  metrics = jax.device_get(metrics)
  summary = metrics
  return summary['loss'], summary['accuracy']


def get_datasets():
  """Load MNIST train and test datasets into memory."""
  train_ds = load_split(tfds.Split.TRAIN)
  test_ds = load_split(tfds.Split.TEST)
  return train_ds, test_ds


def train(train_ds, test_ds):
  """Train MNIST to completion."""
  rng = random.PRNGKey(0)

  batch_size = FLAGS.batch_size
  num_epochs = FLAGS.num_epochs

  optimizers = create_optimizers(random.split(rng, jax.device_count()))

  input_rng = onp.random.RandomState(0)
  test_ds = jax_utils.replicate(test_ds)

  for epoch in range(1, num_epochs + 1):
    optimizers, _ = train_epoch(optimizers, train_ds, batch_size, epoch,
                                input_rng)
    loss, accuracy = eval_model(optimizers.target, test_ds)
    logging.info('eval epoch: %d, loss: %s, accuracy: %s', epoch, loss,
                 accuracy * 100)
  return optimizers


def main(_):
  train_ds, test_ds = get_datasets()
  train(train_ds, test_ds)


if __name__ == '__main__':
  app.run(main)
