# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
A minimal training script for DiT using PyTorch DDP.
"""
import torch

# the first flag below was False when we tested this script but True makes A100 training a lot faster:
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
import argparse
import os
from copy import deepcopy
from functools import partial

import torch.distributed as dist
from colossalai import launch_from_torch
from colossalai.accelerator import get_accelerator
from colossalai.booster import Booster
from colossalai.booster.plugin import LowLevelZeroPlugin
from colossalai.cluster import DistCoordinator
from colossalai.logging import get_dist_logger
from colossalai.utils import get_current_device
from tqdm import tqdm
from transformers import AutoModel

from data_utils import load_datasets, make_batch, preprocess_batch
from diffusion import create_diffusion
from models import DiT_models

#################################################################################
#                             Training Helper Functions                         #
#################################################################################


@torch.no_grad()
def update_ema(ema_model, model, decay=0.9999):
    """
    Step the EMA model towards the current model.
    """

    for ema_p, p in zip(ema_model.parameters(), model.parameters()):
        ema_p.mul_(decay).add_(p.data, alpha=1 - decay)


def requires_grad(model, flag=True):
    """
    Set requires_grad flag for all parameters in a model.
    """
    for p in model.parameters():
        p.requires_grad = flag


def all_reduce_mean(tensor: torch.Tensor) -> torch.Tensor:
    dist.all_reduce(tensor=tensor, op=dist.ReduceOp.SUM)
    tensor.div_(dist.get_world_size())
    return tensor


#################################################################################
#                                  Training Loop                                #
#################################################################################


def main(args):
    """
    Trains a new DiT model.
    """
    launch_from_torch({})
    coordinator = DistCoordinator()
    logger = get_dist_logger()
    plugin = LowLevelZeroPlugin(stage=2, precision="fp16")
    booster = Booster(plugin=plugin)

    if coordinator.is_master():
        os.makedirs(args.checkpoint_dir, exist_ok=True)

    # Setup model
    vqvae = (
        AutoModel.from_pretrained(args.vqvae, trust_remote_code=True)
        .to(get_current_device())
        .eval()
    )
    model = DiT_models[args.model]().to(get_current_device())
    patch_size = model.patch_size
    ema = deepcopy(model)
    requires_grad(ema, False)
    model.train()  # important! This enables embedding dropout for classifier-free guidance
    ema.eval()  # EMA model should always be in eval mode
    if args.grad_checkpoint:
        model.enable_gradient_checkpointing()

    diffusion = create_diffusion(
        timestep_respacing=""
    )  # default: 1000 steps, linear noise schedule

    # Setup optimizer (we used default Adam betas=(0.9, 0.999) and a constant learning rate of 1e-4 in our paper):
    opt = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0)

    # Setup dataloader
    dataset = load_datasets(args.dataset)
    dataloader = plugin.prepare_dataloader(
        dataset,
        batch_size=args.batch_size,
        collate_fn=partial(make_batch, video_dir=args.video_dir),
        shuffle=True,
        drop_last=True,
    )
    logger.info(f"Dataset contains {len(dataset)} samples", ranks=[0])

    # Setup booster
    model, opt, _, dataloader, _ = booster.boost(model, opt, dataloader=dataloader)
    logger.info(
        f"Booster init max device memory: {get_accelerator().max_memory_allocated() / 1024 ** 2:.2f} MB",
        ranks=[0],
    )

    for epoch in range(args.epochs):
        dataloader.sampler.set_epoch(epoch)
        with tqdm(
            desc=f"Epoch {epoch}",
            disable=not coordinator.is_master(),
            total=len(dataloader),
        ) as pbar:
            for step, batch in enumerate(dataloader):
                batch = preprocess_batch(batch, patch_size, vqvae)
                video_inputs = batch.pop("video_latent_states")
                mask = batch.pop("video_padding_mask")
                t = torch.randint(
                    0,
                    diffusion.num_timesteps,
                    (video_inputs.shape[0],),
                    device=video_inputs.device,
                )
                loss_dict = diffusion.training_losses(
                    model, video_inputs, t, batch, mask=mask
                )
                loss = loss_dict["loss"].mean()
                booster.backward(loss, opt)
                opt.step()
                opt.zero_grad()
                update_ema(ema, model)

                loss_no_grad = loss.data
                all_reduce_mean(loss_no_grad)
                pbar.set_postfix({"Loss": f"{loss_no_grad.item():.4f}"})
                pbar.update()

                # Save DiT checkpoint:
                if (
                    args.save_interval > 0 and (step + 1) % args.save_interval == 0
                ) or (step + 1) == len(dataloader):
                    save_path = os.path.join(
                        args.checkpoint_dir, f"epoch-{epoch}-step-{step}"
                    )
                    os.makedirs(save_path, exist_ok=True)
                    booster.save_model(
                        model, os.path.join(save_path, "model"), shard=True
                    )
                    booster.save_optimizer(
                        opt, os.path.join(save_path, "optimizer"), shard=True
                    )
                    if coordinator.is_master():
                        ema_state_dict = ema.state_dict()
                        for k, v in ema_state_dict.items():
                            ema_state_dict[k] = v.cpu()
                        torch.save(ema_state_dict, os.path.join(save_path, "ema.pt"))
                    dist.barrier()
                    logger.info(f"Saved checkpoint to {save_path}", ranks=[0])

                get_accelerator().empty_cache()
    logger.info(
        f"Training complete, max device memory: {get_accelerator().max_memory_allocated() / 1024 ** 2:.2f} MB",
        ranks=[0],
    )


if __name__ == "__main__":
    # Default args here will train DiT-XL/2 with the hyperparameters we used in our paper (except training iters).
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-m", "--model", type=str, choices=list(DiT_models.keys()), default="DiT-S/8"
    )
    parser.add_argument("-d", "--dataset", nargs="+", default=[])
    parser.add_argument("-v", "--video_dir", type=str, required=True)
    parser.add_argument("-e", "--epochs", type=int, default=10)
    parser.add_argument("-b", "--batch_size", type=int, default=4)
    parser.add_argument("-g", "--grad_checkpoint", action="store_true", default=False)
    parser.add_argument("--save_interval", type=int, default=20)
    parser.add_argument("--checkpoint_dir", type=str, default="checkpoints")
    parser.add_argument("--vqvae", default="hpcai-tech/vqvae")
    args = parser.parse_args()
    main(args)
