# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

import copy
import os
import pathlib
from typing import Type

import pytest
import torch

from composer import Algorithm, Trainer
from composer.algorithms import SAM, SWA, GyroDropout, LayerFreezing, SeqLengthWarmup, StochasticDepth
from composer.utils import dist
from tests.algorithms.algorithm_settings import get_alg_dataloader, get_alg_kwargs, get_alg_model, get_algs_with_marks
from tests.common import deep_compare
from tests.common.markers import world_size


@pytest.mark.gpu
@pytest.mark.parametrize('alg_cls', get_algs_with_marks())
@pytest.mark.filterwarnings('ignore:Detected call of `lr_scheduler.step()'
                           )  # optimizer.step() sometimes skipped when NaN/inf on low batch size
@world_size(1, 2)
def test_algorithm_resumption(
    tmp_path: pathlib.Path,
    alg_cls: Type[Algorithm],
    world_size,
):
    folder1 = os.path.join(tmp_path, 'folder1')
    folder2 = os.path.join(tmp_path, 'folder2')
    os.makedirs(folder1, exist_ok=True)
    os.makedirs(folder2, exist_ok=True)

    model = get_alg_model(alg_cls)
    alg_kwargs = get_alg_kwargs(alg_cls)

    copied_model = copy.deepcopy(model)  # copy the model so the params will start from the same point

    if alg_cls is LayerFreezing:
        pytest.xfail('Known issues')

    if alg_cls in (SAM, StochasticDepth):
        pytest.xfail('Mismatch in weights when resuming from a checkpoint.')

    if alg_cls is GyroDropout:
        pytest.xfail('GyroDropoutLayer is not implemented in a way that allows correct resumption.')

    if alg_cls is SWA and world_size > 1:
        pytest.xfail('SWA is not implemented in a way that is compatible correct resumption on multiple devices.')

    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=5)

    shared_config = {
        'max_duration': '2ep',
        'save_filename': 'ep{epoch}-rank{rank}',
        'save_interval': '1ep',
        'train_subset_num_batches': 2,
        'precision': 'amp_bf16',
    }
    train_dataloader = get_alg_dataloader(alg_cls) if world_size == 1 else get_alg_dataloader(alg_cls, multigpu=True)
    # train model once, saving checkpoints every epoch
    trainer1 = Trainer(
        model=model,
        train_dataloader=train_dataloader,
        optimizers=optimizer,
        schedulers=scheduler,
        save_folder=folder1,
        algorithms=alg_cls(**alg_kwargs),
        **shared_config,
    )
    trainer1.fit()

    # create second trainer, load an intermediate checkpoint
    # and continue training

    optimizer = torch.optim.Adam(copied_model.parameters(), lr=0.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=5)

    alg = alg_cls(**alg_kwargs)
    # SeqLengthWarmup has a call to ._activate_model() that happens on the first call to the algorithm
    # in order to get complete matching of the rng state, we have to cause that extra call to be skipped
    # when reloading.
    if alg_cls is SeqLengthWarmup:
        alg._activated = True  # type: ignore

    train_dataloader = get_alg_dataloader(alg_cls) if world_size == 1 else get_alg_dataloader(alg_cls, multigpu=True)
    trainer2 = Trainer(
        model=copied_model,
        train_dataloader=train_dataloader,
        load_path=os.path.join(folder1, 'ep1-rank{rank}'),
        load_weights_only=False,
        load_strict_model_weights=False,
        optimizers=optimizer,
        schedulers=scheduler,
        save_folder=folder2,
        algorithms=alg,
        **shared_config,
    )
    trainer2.fit()
    # check that the checkpoints are equal
    if world_size == 1 or dist.get_global_rank() == 0:
        _assert_checkpoints_equal(
            file1=os.path.join(folder1, 'ep2-rank0'),
            file2=os.path.join(folder2, 'ep2-rank0'),
        )

    # check that different epoch checkpoints are _not_ equal
    # this ensures that the model weights are being updated.
    if world_size == 1 or dist.get_global_rank() == 0:
        with pytest.raises(AssertionError):
            _assert_model_weights_equal(
                file1=os.path.join(folder1, 'ep1-rank0'),
                file2=os.path.join(folder1, 'ep2-rank0'),
            )


def _assert_checkpoints_equal(file1, file2):
    # TODO: consider merging with _assert_checkpoints_equivalent
    checkpoint1 = torch.load(file1)
    checkpoint2 = torch.load(file2)

    # compare rng
    deep_compare(checkpoint1['rng'], checkpoint2['rng'])

    # compare state
    # remove the wall clock time fields since they will always differ
    del checkpoint1['state']['timestamp']['Timestamp']['total_wct']
    del checkpoint1['state']['timestamp']['Timestamp']['iteration_wct']
    del checkpoint1['state']['timestamp']['Timestamp']['epoch_wct']
    del checkpoint1['state']['timestamp']['Timestamp']['batch_wct']
    del checkpoint2['state']['timestamp']['Timestamp']['total_wct']
    del checkpoint2['state']['timestamp']['Timestamp']['iteration_wct']
    del checkpoint2['state']['timestamp']['Timestamp']['epoch_wct']
    del checkpoint2['state']['timestamp']['Timestamp']['batch_wct']

    # delete run_name since its time dependent
    del checkpoint1['state']['run_name']
    del checkpoint2['state']['run_name']

    # Remove all saved checkpoints to timestamp (accumulates between runs)
    del checkpoint1['state']['callbacks']['CheckpointSaver']['all_saved_checkpoints_to_timestamp']
    del checkpoint2['state']['callbacks']['CheckpointSaver']['all_saved_checkpoints_to_timestamp']

    # Remove algorithm representations which are memory addresses
    for i, algo_info in enumerate(checkpoint1['state']['algorithms']):
        if '0x' in algo_info[1]['repr']:
            del checkpoint1['state']['algorithms'][i]
    for i, algo_info in enumerate(checkpoint2['state']['algorithms']):
        if '0x' in algo_info[1]['repr']:
            del checkpoint2['state']['algorithms'][i]

    deep_compare(checkpoint1['state'], checkpoint2['state'])


def _assert_model_weights_equal(file1, file2):
    checkpoint1 = torch.load(file1)
    checkpoint2 = torch.load(file2)

    deep_compare(checkpoint1['state']['model'], checkpoint2['state']['model'])
