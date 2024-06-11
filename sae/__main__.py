from contextlib import nullcontext, redirect_stdout
from dataclasses import dataclass
import os

import torch
import torch.distributed as dist
from datasets import load_dataset
from simple_parsing import field, parse
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from .data import chunk_and_tokenize
from .trainer import SaeTrainer, TrainConfig


@dataclass
class RunConfig(TrainConfig):
    model: str = field(
        default="EleutherAI/pythia-160m",
        positional=True,
    )
    """Name of the model to train."""

    dataset: str = field(
        default="togethercomputer/RedPajama-Data-1T-Sample",
        positional=True,
    )
    """Path to the dataset to use for training."""

    split: str = "train"
    """Dataset split to use for training."""

    ctx_len: int = 2048
    """Context length to use for training."""


def run():
    local_rank = os.environ.get("LOCAL_RANK")
    ddp = local_rank is not None
    rank = int(local_rank) if ddp else 0

    if ddp:
        torch.cuda.set_device(int(local_rank))
        dist.init_process_group("nccl")

        if rank == 0:
            print(f"Using DDP across {dist.get_world_size()} GPUs.")

    args = parse(RunConfig)
    if args.load_in_8bit:
        dtype = torch.float16
    elif torch.cuda.is_bf16_supported():
        dtype = torch.bfloat16
    else:
        dtype = "auto"

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        attn_implementation="sdpa",
        device_map={"": f"cuda:{rank}"},
        quantization_config=BitsAndBytesConfig(load_in_8bit=args.load_in_8bit),
        torch_dtype=dtype,
        token=args.hf_token,
    )

    dataset = load_dataset(
        args.dataset,
        split=args.split,
        # TODO: Maybe set this to False by default? But RPJ requires it.
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model)

    # TODO: Only do the chunking and tokenization on rank 0
    tokenized = chunk_and_tokenize(dataset, tokenizer, max_seq_len=args.ctx_len)
    if ddp:
        tokenized = tokenized.shard(dist.get_world_size(), rank)

    # Prevent ranks other than 0 from printing
    with nullcontext() if rank == 0 else redirect_stdout(None):
        print(f"Training on '{args.dataset}' (split '{args.split}')")
        print(f"Storing model weights in {model.dtype}")

        trainer = SaeTrainer(args, tokenized, model)
        trainer.fit()


if __name__ == "__main__":
    run()