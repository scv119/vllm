from typing import Tuple

import torch

from torch import nn
import torch.nn.functional as F

import triton
import triton.language as tl

from vllm._C import ops
from vllm.model_executor.layers.linear import (
    ReplicatedLinear,
    RowParallelLinear,
    ColumnParallelLinear)

from vllm.model_executor.parallel_utils.parallel_state import (
    get_tensor_model_parallel_rank)
from vllm.model_executor.parallel_utils.communication_op import (
    tensor_model_parallel_all_reduce)
from vllm.model_executor.utils import set_weight_attrs


class MoEMLP(nn.Module):
    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
    ) -> None:
        super().__init__()
        self.ffn_dim = intermediate_size
        self.hidden_dim = hidden_size

        self.w1 = ColumnParallelLinear(self.hidden_dim,
                                   self.ffn_dim,
                                   bias=False)
        self.w2 = RowParallelLinear(self.ffn_dim,
                                   self.hidden_dim,
                                   bias=False)
        self.w3 = ColumnParallelLinear(self.hidden_dim,
                                   self.ffn_dim,
                                   bias=False)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        assert False, "Not implemented yet"

class MoE(nn.Module):

    def __init__(
        self,
        num_experts: int,
        top_k: int,
        hidden_size: int,
        intermediate_size: int,
        tp_size: int,
    ):
        super().__init__()
        self.num_total_experts = num_experts
        self.top_k = top_k
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size // tp_size

        self.gate = ReplicatedLinear(self.hidden_size,
                                     self.num_total_experts,
                                     bias=False,
                                     linear_method=None)

        self.experts = nn.ModuleList([
            MoEMLP(self.hidden_size,
                   self.intermediate_size)
            for _ in range(self.num_total_experts)
        ])

        self.expert_w1s = [expert.w1.weight.T() for expert in self.experts]
        self.expert_w2s = [expert.w2.weight.T() for expert in self.experts]
        self.expert_w3s = [expert.w3.weight.T() for expert in self.experts]

    def weight_loader(self, param: nn.Parameter, loaded_weight: torch.Tensor, expert_id: int):
        tp_rank = get_tensor_model_parallel_rank()
        loaded_weight = loaded_weight.transpose(0, 1)
        parallel_dim = getattr(param, "parallel_dim", 0)
        param_data = param.data
        shard_size = param_data.shape[parallel_dim + 1]
        start_idx = tp_rank * shard_size
        loaded_weight = loaded_weight.narrow(parallel_dim, start_idx,
                                                shard_size)
        assert param_data[expert_id].shape == loaded_weight.shape
        param_data[expert_id].copy_(loaded_weight)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        batch_size, sequence_length, hidden_size = hidden_states.shape
        hidden_states = hidden_states.view(-1, self.hidden_size)
        # router_logits: (batch * sequence_length, n_experts)
        router_logits, _ = self.gate(hidden_states)

        routing_weights = F.softmax(router_logits, dim=1, dtype=torch.float)
        routing_weights, selected_experts = torch.topk(routing_weights,
                                                       self.top_k,
                                                       dim=-1)
        routing_weights /= routing_weights.sum(dim=-1, keepdim=True)

        expanded_hidden_states, experts_range, expanded_weights, experts_indices = \
            self.expand_and_permutate_hidden_states(
                hidden_states, selected_experts, routing_weights)
        print(f"{expanded_weights=} {experts_range=} {experts_indices=} {selected_experts=} {routing_weights=}") 

        expanded_hidden_states = self.grouped_mlp(expanded_hidden_states,
                                                  experts_range)
        expanded_hidden_states.mul_(expanded_weights.unsqueeze(-1))

        tensor_model_parallel_all_reduce(expanded_hidden_states)

        return self.merge_expert_outputs(expanded_hidden_states,
                                         experts_indices).view(
                                             batch_size, sequence_length,
                                             hidden_size)

    def expand_and_permutate_hidden_states(
        self,
        hidden_states: torch.Tensor,  # [batch_size, hidden_size]
        selected_experts: torch.Tensor,  # [batch_size, top_k_experts]
        routing_weights: torch.Tensor,  # [batch_size, top_k_experts]
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        _, experts_indices = torch.sort(selected_experts.view(-1), dim=-1)
        cum_experts_range = torch.zeros(self.num_total_experts + 1,
                                        dtype=torch.int32,
                                        device=hidden_states.device)
        num_rows_per_expert = torch.zeros(self.num_total_experts,
                                          dtype=torch.int32,
                                          device=hidden_states.device)
        ops.bincount(selected_experts.view(-1), num_rows_per_expert)
        torch.cumsum(num_rows_per_expert, dim=0, out=cum_experts_range[1:])
        expanded_weights = routing_weights.view(-1)[experts_indices]
        return hidden_states[experts_indices.div_(
            self.top_k, rounding_mode="floor"
        )], cum_experts_range, expanded_weights, experts_indices

    def grouped_mlp(
        self,
        expanded_hidden_states: torch.
        Tensor,  # [batch_size * top_k_experts, hidden_size]
        cum_experts_range: torch.Tensor,  # [num_experts + 1]
    ) -> torch.Tensor:  # [batch_size * top_k_experts, hidden_size]
        grouped_w1_out = grouped_matmul(expanded_hidden_states,
                                        cum_experts_range, self.expert_w1s, "silu")
        grouped_w3_out = grouped_matmul(expanded_hidden_states,
                                        cum_experts_range, self.expert_w3s)
        grouped_w1_out.mul_(grouped_w3_out)
        return grouped_matmul(grouped_w1_out, cum_experts_range, self.expert_w2s)

    def merge_expert_outputs(
            self,
            expanded_hidden_states: torch.
        Tensor,  # [batch_size * top_k_experts, hidden_size]
            expert_indicies,  # [batch_size * top_k_experts]
    ) -> torch.Tensor:
        out = torch.zeros(expanded_hidden_states.shape[0] // self.top_k,
                          self.hidden_size,
                          device=expanded_hidden_states.device,
                          dtype=expanded_hidden_states.dtype)
        out.index_add_(0, expert_indicies, expanded_hidden_states)
        return out


@triton.jit
def grouped_matmul_kernel(
    # device tensor of matrices pointers
    fused_input_ptr,
    cum_input_group_range,
    fused_b_ptr,
    fused_output_ptr,
    group_size,
    batch_size,
    n,
    k,
    lda,
    ldb,
    ldc,
    # number of virtual SM
    NUM_SM: tl.constexpr,
    # tile sizes
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
    BLOCK_SIZE_K: tl.constexpr,
    ACTIVATION: tl.constexpr,
):
    tile_idx = tl.program_id(0)
    last_problem_end = 0
    for g in range(group_size):
        # get the gemm size of the current problem
        a_offset = tl.load(cum_input_group_range + g)
        gm = tl.load(cum_input_group_range + g + 1) - a_offset
        gn = n
        gk = k
        num_m_tiles = tl.cdiv(gm, BLOCK_SIZE_M)
        num_n_tiles = tl.cdiv(gn, BLOCK_SIZE_N)
        num_tiles = num_m_tiles * num_n_tiles
        # iterate through the tiles in the current gemm problem
        while (tile_idx >= last_problem_end
               and tile_idx < last_problem_end + num_tiles):

            # pick up a tile from the current gemm problem
            k = gk
            a_ptr = fused_input_ptr + a_offset * lda
            b_ptr = fused_b_ptr + g * k * n
            c_ptr = fused_output_ptr + a_offset * ldc
            # figure out tile coordinates
            tile_idx_in_gemm = tile_idx - last_problem_end
            tile_m_idx = tile_idx_in_gemm // num_n_tiles
            tile_n_idx = tile_idx_in_gemm % num_n_tiles

            # do regular gemm here
            offs_am = tile_m_idx * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
            offs_bn = tile_n_idx * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
            offs_k = tl.arange(0, BLOCK_SIZE_K)
            a_ptrs = a_ptr + offs_am[:, None] * lda + offs_k[None, :]
            b_ptrs = b_ptr + offs_k[:, None] * ldb + offs_bn[None, :]
            accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N),
                                   dtype=tl.float32)
            for kk in range(0, tl.cdiv(k, BLOCK_SIZE_K)):
                # hint to Triton compiler to do proper loop pipelining
                tl.multiple_of(a_ptrs, [16, 16])
                tl.multiple_of(b_ptrs, [16, 16])

                a = tl.load(a_ptrs,
                            mask=offs_k[None, :] < k - kk * BLOCK_SIZE_K,
                            other=0.0)
                b = tl.load(b_ptrs,
                            mask=offs_k[:, None] < k - kk * BLOCK_SIZE_K,
                            other=0.0)
                accumulator += tl.dot(a, b)
                a_ptrs += BLOCK_SIZE_K
                b_ptrs += BLOCK_SIZE_K * ldb

            if ACTIVATION == "silu":
                accumulator = silu(accumulator)
            c = accumulator.to(tl.float16)

            offs_cm = tile_m_idx * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
            offs_cn = tile_n_idx * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
            c_ptrs = c_ptr + ldc * offs_cm[:, None] + offs_cn[None, :]
            c_mask = (offs_cm[:, None] < gm) & (offs_cn[None, :] < gn)

            tl.store(c_ptrs, c, mask=c_mask)

            # go to the next tile by advancing NUM_SM
            tile_idx += NUM_SM

        # get ready to go to the next gemm problem
        last_problem_end = last_problem_end + num_tiles


@triton.jit
def silu(x):
    return x * tl.sigmoid(x)


def grouped_matmul(fused_input: torch.Tensor,
                   cum_group_range: torch.Tensor,
                   fused_group_b: torch.Tensor,
                   activation: str = ""):
    device = torch.device('cuda')
    assert cum_group_range.shape[0] == fused_group_b.shape[0] + 1
    group_size = cum_group_range.shape[0] - 1
    output = torch.zeros(fused_input.shape[0],
                         fused_group_b.shape[2],
                         device=device,
                         dtype=fused_input.dtype)
    BLOCK_SIZE_M = 16
    BLOCK_SIZE_N = 64
    BLOCK_SIZE_K = 32
    num_warps = 2
    NUM_SM = 128
    num_stages = 5
    if fused_input.shape[0] >= 8:
        num_warps = 4
        BLOCK_SIZE_N = 128
    if fused_input.shape[0] >= 32:
        num_warps = 4
        BLOCK_SIZE_M = 32
        BLOCK_SIZE_N = 128
    # we use a fixed number of CTA, and it's auto-tunable
    grid = lambda META: (META['NUM_SM'], )
    grouped_matmul_kernel[grid](fused_input,
                                cum_group_range,
                                fused_group_b,
                                output,
                                group_size,
                                batch_size=fused_input.shape[0],
                                n=fused_group_b.shape[2],
                                k=fused_group_b.shape[1],
                                lda=fused_input.stride(0),
                                ldb=fused_group_b.stride(1),
                                ldc=output.stride(0),
                                ACTIVATION=activation,
                                BLOCK_SIZE_M=BLOCK_SIZE_M,
                                BLOCK_SIZE_N=BLOCK_SIZE_N,
                                BLOCK_SIZE_K=BLOCK_SIZE_K,
                                NUM_SM=NUM_SM,
                                num_warps=num_warps,
                                num_stages=num_stages),

    return output
