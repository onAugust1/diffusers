import os
import random

os.environ["ONEFLOW_MLIR_CSE"] = "1"
os.environ["ONEFLOW_MLIR_ENABLE_INFERENCE_OPTIMIZATION"] = "1"
os.environ["ONEFLOW_MLIR_ENABLE_ROUND_TRIP"] = "1"
os.environ["ONEFLOW_MLIR_FUSE_FORWARD_OPS"] = "1"
os.environ["ONEFLOW_MLIR_FUSE_OPS_WITH_BACKWARD_IMPL"] = "1"
os.environ["ONEFLOW_MLIR_GROUP_MATMUL"] = "1"
os.environ["ONEFLOW_MLIR_PREFER_NHWC"] = "1"

os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_CONV_BIAS"] = "1"
os.environ["ONEFLOW_KERNEL_ENABLE_FUSED_LINEAR"] = "1"

os.environ["ONEFLOW_KERNEL_CONV_CUTLASS_IMPL_ENABLE_TUNING_WARMUP"] = "1"
os.environ["ONEFLOW_KERNEL_CONV_ENABLE_CUTLASS_IMPL"] = "1"

os.environ["ONEFLOW_CONV_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"
os.environ["ONEFLOW_MATMUL_ALLOW_HALF_PRECISION_ACCUMULATION"] = "1"

os.environ["ONEFLOW_LINEAR_EMBEDDING_SKIP_INIT"] = "1"

import click

# cv2 must be imported before diffusers and oneflow to avlid error: AttributeError: module 'cv2.gapi' has no attribute 'wip'
# Maybe bacause oneflow use a lower version of cv2
import cv2
import oneflow as flow
import torch
from tqdm import tqdm
from dataclasses import dataclass, fields
from onediff.infer_compiler import oneflow_compile


@dataclass
class TensorInput(object):
    noise: torch.float16
    time: torch.int64
    cross_attention_dim: torch.float16

    @classmethod
    def gettype(cls, key):
        field_types = {field.name: field.type for field in fields(TensorInput)}
        return field_types[key]


def get_unet(token, _model_id, revision):
    from diffusers import UNet2DConditionModel

    unet = UNet2DConditionModel.from_pretrained(
        _model_id,
        use_auth_token=token,
        revision=revision,
        torch_dtype=torch.float16,
        subfolder="unet",
    )
def warmup_with_arg(graph, arg_meta_of_sizes, added):
    for arg_metas in arg_meta_of_sizes:
        print(f"warmup {arg_metas=}")
        arg_tensors = [
            torch.empty(arg_metas.noise, dtype=arg_metas.gettype("noise")).to("cuda"),
            torch.empty(arg_metas.time, dtype=arg_metas.gettype("time")).to("cuda"),
            torch.empty(
                arg_metas.cross_attention_dim,
                dtype=arg_metas.gettype("cross_attention_dim"),
            ).to("cuda"),
        ]
        graph(*arg_tensors, added)  # build and warmup


def img_dim(i, start, stride):
    return start + stride * i


def noise_shape(batch_size, num_channels, image_w, image_h):
    sizes = (image_w // 8, image_h // 8)
    return (batch_size, num_channels) + sizes


def get_arg_meta_of_sizes(
    batch_sizes,
    resolution_scales,
    num_channels,
    cross_attention_dim,
    start=768,
    stride=128,
):
    return [
        TensorInput(
            noise_shape(
                batch_size,
                num_channels,
                img_dim(i, start, stride),
                img_dim(j, start, stride),
            ),
            (1,),
            (batch_size, 77, cross_attention_dim),
        )
        for batch_size in batch_sizes
        for i in resolution_scales
        for j in resolution_scales
    ]


@click.command()
@click.option("--token")
@click.option("--repeat", default=1000)
@click.option("--sync_interval", default=50)
@click.option("--save", is_flag=True)
@click.option("--load", is_flag=True)
@click.option("--file", type=str, default="./unet_graphs")
@click.option(
   "--model_id", type=str, default="stabilityai/stable-diffusion-xl-base-1.0"
)
@click.option("--revision", type=str, default="fp16")
def benchmark(token, repeat, sync_interval, save, load, file, model_id, revision):
    RESOLUTION_SCALES = [2, 1, 0]
    BATCH_SIZES = [2]
    # TODO: reproduce bug caused by changing batch
    # BATCH_SIZES = [4, 2]

    unet = get_unet(token, model_id, revision)
    unet_graph = oneflow_compile(unet)

    num_channels = 4
    cross_attention_dim = unet.config["cross_attention_dim"]
    from diffusers.utils import floats_tensor
    import torch

    if (
        model_id == "stabilityai/stable-diffusion-xl-base-1.0"
        or "xl-base-1.0" in model_id
    ):
        # sdxl needed
        add_text_embeds = floats_tensor((2, 1280)).to("cuda").to(torch.float16)
        add_time_ids = floats_tensor((2, 6)).to("cuda").to(torch.float16)
        added_cond_kwargs = {"text_embeds": add_text_embeds, "time_ids": add_time_ids}
    else:
        added_cond_kwargs = None

    warmup_meta_of_sizes = get_arg_meta_of_sizes(
        batch_sizes=BATCH_SIZES,
        resolution_scales=RESOLUTION_SCALES,
        num_channels=num_channels,
        cross_attention_dim=cross_attention_dim,
    )
    for i, m in enumerate(warmup_meta_of_sizes):
        print(f"warmup case #{i + 1}:", m)

    import time

    if load == True:

        print("loading graphs...")
        t0 = time.time()
        unet_graph.warmup_with_load(file)
        t1 = time.time()
        duration = t1 - t0
        print(f"Finish in {duration:.3f} seconds")
    else:
        print("warmup with arguments...")
        warmup_with_arg(unet_graph, warmup_meta_of_sizes, added_cond_kwargs)

    if save:
        print("saving graphs...")
        unet_graph.save_graph(file)

if __name__ == "__main__":
    print(f"{flow.__path__=}")
    print(f"{flow.__version__=}")
    benchmark()