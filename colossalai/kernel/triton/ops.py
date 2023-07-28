from typing import Optional, Tuple

import torch
from torch import nn
from torch.nn import functional as F

try:
    import triton
    import triton.language as tl
    HAS_TRITON = True
except ImportError:
    HAS_TRITON = False
    print("please install triton from https://github.com/openai/triton")

if HAS_TRITON:
    from .qkv_matmul_kernel import qkv_gemm_4d_kernel, qkv_gemm_4d_kernel_alibi
    from .softmax_kernel import softmax_kernel

    def self_attention_forward_without_fusion(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, input_mask: torch.Tensor, scale: float):
        r""" A function to do QKV Attention calculation by calling GEMM and softmax triton kernels 
        Args:
            q (torch.Tensor): Q embedding in attention layer, shape should be (batch, seq_len, num_heads, head_size)
            k (torch.Tensor): K embedding in attention layer, shape should be (batch, seq_len, num_heads, head_size)
            v (torch.Tensor): V embedding in attention layer, shape should be (batch, seq_len, num_heads, head_size)
            input_mask (torch.Tensor): mask for softmax layer, shape should be (batch, num_heads, seq_lem, seq_len)
            scale: the float scale value which is used to multiply with Q*K^T before doing softmax

        Return:
            output (Torch.Tensor): The output shape is (batch, seq_len, num_heads, head_size)
        """
        assert len(q.shape) == 4, "the shape of q val must be 4"
        batches, M, H, K = q.shape
        assert q.shape == k.shape, "the shape of q and the shape of k must be equal"
        assert q.shape == v.shape, "the shape of q and the shape of v must be equal"
        assert q.shape[-1] == k.shape[-1], "the last dimension of q and k must be equal"

        N = k.shape[1]

        # head_size * num_of_head
        d_model = q.shape[-1] * q.shape[-2]

        score_output = torch.empty(
            (batches, H, M, N), device=q.device, dtype=q.dtype)

        grid = lambda meta: (
            batches,
            H,
            triton.cdiv(M, meta["BLOCK_SIZE_M"]) *
            triton.cdiv(N, meta["BLOCK_SIZE_N"]),
        )

        qkv_gemm_4d_kernel[grid](
            q, k, score_output,
            M, N, K,
            q.stride(0), q.stride(2), q.stride(1), q.stride(3),
            k.stride(0), k.stride(2), k.stride(3), k.stride(1),
            score_output.stride(0), score_output.stride(1), score_output.stride(2), score_output.stride(3),
            scale=scale,
            # currently manually setting, later on we can use auto-tune config to match best setting
            BLOCK_SIZE_M=64,
            BLOCK_SIZE_N=32,
            BLOCK_SIZE_K=32,
            GROUP_SIZE_M=8,
        )
        
        softmax_output = torch.empty(
            score_output.shape, device=score_output.device, dtype=score_output.dtype)
        score_output_shape = score_output.shape

        score_output = score_output.view(-1, score_output.shape[-1])
        n_rows, n_cols = score_output.shape

        if n_rows <= 350000:
            
            block_size = max(triton.next_power_of_2(n_cols), 2)
            num_warps = 4
            if block_size >= 4096:
                num_warps = 16
            elif block_size >= 2048:
                num_warps = 8
            else:
                num_warps = 4

            softmax_kernel[(n_rows, )](
                softmax_output,
                score_output,
                score_output.stride(0),
                n_cols,
                mask_ptr = input_mask,
                num_warps=num_warps,
                BLOCK_SIZE=block_size,
            )

        else:
            #TODO: change softmax kernel functions to make it suitable for large size dimension
            softmax_output = torch.nn.functional.softmax(score_output, dim=-1)
            softmax_output = softmax_output.view(*score_output_shape)

        batches, H, M, K = softmax_output.shape
        N = v.shape[-1]

        output = torch.empty(
            (batches, M, H, N), device=softmax_output.device, dtype=softmax_output.dtype)

        grid = lambda meta: (
            batches,
            H,
            triton.cdiv(M, meta["BLOCK_SIZE_M"]) *
            triton.cdiv(N, meta["BLOCK_SIZE_N"]),
        )

        qkv_gemm_4d_kernel[grid](
            softmax_output, v, output,
            M, N, K,
            softmax_output.stride(0),
            softmax_output.stride(1),
            softmax_output.stride(2),
            softmax_output.stride(3),
            v.stride(0),
            v.stride(2),
            v.stride(1),
            v.stride(3),
            output.stride(0),
            output.stride(2),
            output.stride(1),
            output.stride(3),
            BLOCK_SIZE_M=128,
            BLOCK_SIZE_N=64,
            BLOCK_SIZE_K=64,
            GROUP_SIZE_M=8,
            scale=-1,
        )
        return output.view(batches, -1, d_model)
    
    def compute_attention_for_bloom(q: torch.Tensor, 
                                    k: torch.Tensor, 
                                    v: torch.Tensor, 
                                    alibi: torch.Tensor, 
                                    beta: torch.float32 = 1,
                                    scale: torch.float32 = 1.2,
                                    attention_mask: torch.Tensor = None,
                                    drop_out: torch.float32 = -1, 
                                    head_mask: Optional[torch.Tensor] = None,
                                    layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
                                    use_cache: bool = True
                                ):
        r""" A function to do QKV Attention calculation by calling GEMM and softmax triton kernels to used for bloom attention
        Args:
            q (torch.Tensor): Q embedding in attention layer, shape should be (batch, num_heads, seq_len, head_size)
            k (torch.Tensor): K embedding in attention layer, shape should be (batch, num_heads, head_size, kv_length)
            v (torch.Tensor): V embedding in attention layer, shape should be (batch, num_heads, kv_length, head_size)
            alibi(torch.Tensor): bias for qk^T GEMM, shape should be (batch, H, q_length, kv_length)
            input_mask (torch.Tensor): mask for softmax layer, shape should be (batch, num_heads, seq_lem, seq_len)
            scale: the float scale value which is used to multiply with Q*K^T before doing softmax
            beta: the float value for alibi bias matrix 

        Return:
            output (Torch.Tensor): The output shape is (batch, seq_len, num_heads, head_size)
        """
        
        assert len(q.shape) == len(k.shape), "the dimensions must be matched"
        assert len(q.shape) == len(v.shape), "the dimensions must be matched"
        assert len(q.shape) == 4, "the length of input q must be 4, which is (batch, seq_len, num_heads, head_dim)"

        batches, H, M, K = q.shape
        d_model = q.shape[1] * q.shape[3]

        # k shape: (batches, num_heads, head_dim (K), seq_k(N))
        N = k.shape[-1]

        score_output = torch.empty(
            (batches, H, M, N), device=q.device, dtype=q.dtype)

        assert len(score_output) == len(alibi), "the length of alibi and score output should be matched"
        assert score_output.shape[:-1] == alibi.shape[:-1], "the shape of alibi and score outout also should be the same"
        alibi = alibi.expand(batches, H, M, N)

        grid = lambda meta: (
            batches,
            H,
            triton.cdiv(M, meta["BLOCK_SIZE_M"]) *
            triton.cdiv(N, meta["BLOCK_SIZE_N"]),
        )

        qkv_gemm_4d_kernel[grid](
            q, k, 
            score_output,
            M, N, K,
            q.stride(0), q.stride(1), q.stride(2), q.stride(3),
            k.stride(0), k.stride(1), k.stride(2), k.stride(3),
            score_output.stride(0), score_output.stride(1), score_output.stride(2), score_output.stride(3),
            scale=scale,
            # currently manually setting, later on we can use auto-tune config to match best setting
            BLOCK_SIZE_M=64,
            BLOCK_SIZE_N=32,
            BLOCK_SIZE_K=32,
            GROUP_SIZE_M=8,
            num_stages=4,
        )

        score_output += beta * alibi

        # cast attention scores to fp32, compute scaled softmax and cast back to initial dtype - [batch_size, num_heads, q_length, kv_length]
        input_dtype = score_output.dtype
        # `float16` has a minimum value of -65504.0, whereas `bfloat16` and `float32` have a minimum value of `-3.4e+38`
        if input_dtype == torch.float16:
            score_output = score_output.to(torch.float)

        if attention_mask is not None:
            score_output = torch.masked_fill(score_output, attention_mask, torch.finfo(score_output.dtype).min)
        
        softmax_leading_size = batches * H * M

        if softmax_leading_size <= 350000:
            score_output = score_output.to(input_dtype)
            softmax_output = torch.empty(
                score_output.shape, device=score_output.device, dtype=score_output.dtype)
            score_output_shape = score_output.shape

            score_output = score_output.view(-1, score_output.shape[-1])
            n_rows, n_cols = score_output.shape

            block_size = max(triton.next_power_of_2(n_cols), 2)
            num_warps = 4
            if block_size >= 4096:
                num_warps = 16
            elif block_size >= 2048:
                num_warps = 8
            else:
                num_warps = 4

            softmax_kernel[(n_rows, )](
                softmax_output,
                score_output,
                score_output.stride(0),
                n_cols,
                mask_ptr = None,
                num_warps=num_warps,
                BLOCK_SIZE=block_size,
            )
        else:
            # TODO： fix softmax layer to make kernel to be suitable for large size cases
            softmax_output = F.softmax(score_output, dim=-1, dtype=torch.float32).to(input_dtype)

        if drop_out > 0 and drop_out < 1:
            softmax_output = F.dropout(softmax_output, drop_out, False, False).to(input_dtype)
        
        if head_mask is not None:
            softmax_output = softmax_output * head_mask
            softmax_output = softmax_output.to(input_dtype)
        
        batches, H, M, K = softmax_output.shape
        N = v.shape[-1]

        output = torch.empty(
            (batches, M, H, N), device=softmax_output.device, dtype=softmax_output.dtype)
        
        grid = lambda meta: (
                batches,
                H,
                triton.cdiv(M, meta["BLOCK_SIZE_M"]) *
                triton.cdiv(N, meta["BLOCK_SIZE_N"]),
            )

        qkv_gemm_4d_kernel[grid](
                softmax_output, v, output,
                M, N, K,
                softmax_output.stride(0),
                softmax_output.stride(1),
                softmax_output.stride(2),
                softmax_output.stride(3),
                v.stride(0),
                v.stride(1),
                v.stride(2),
                v.stride(3),
                output.stride(0),
                output.stride(2),
                output.stride(1),
                output.stride(3),
                BLOCK_SIZE_M=64,
                BLOCK_SIZE_N=32,
                BLOCK_SIZE_K=32,
                GROUP_SIZE_M=8,
                scale=-1,
            )
        
        return output.view(batches, -1 , d_model)


    def self_attention_compute_using_triton(qkv,
                                            input_mask,
                                            layer_past,
                                            alibi,
                                            scale,
                                            head_size,
                                            triangular=False,
                                            use_flash=False):

        assert qkv.is_contiguous()
        assert alibi is None, "current triton self-attention does not support alibi"
        batches = qkv.shape[0]
        d_model = qkv.shape[-1] // 3
        num_of_heads = d_model // head_size

        q = qkv[:, :, :d_model]
        k = qkv[:, :, d_model:d_model * 2]
        v = qkv[:, :, d_model * 2:]
        q = q.view(batches, -1, num_of_heads, head_size)
        k = k.view(batches, -1, num_of_heads, head_size)
        v = v.view(batches, -1, num_of_heads, head_size)

        data_output_triton = self_attention_forward_without_fusion(
            q, k, v, input_mask, scale)

        return data_output_triton


    def softmax(input: torch.Tensor, mask: torch.Tensor = None, dim=-1) -> torch.Tensor:
        if mask is not None:
            assert input[-1] == mask[-1], "the last dimentions should be the same for input and mask"
        assert dim == -1 or dim == len(input.shape)-1, "currently softmax layer only support last dimention"
        
        hidden_dim = input.shape[-1]
        output = torch.empty_like(input)
        input = input.view(-1, hidden_dim)
        if mask is not None: 
            mask = mask.view(-1, hidden_dim)
            assert input.shape[0] == mask.shape[0], "the fist dimention of mask and input should be the same"

        num_rows, num_cols = input.shape
        block_size = max(triton.next_power_of_2(num_cols), 2)
        num_warps = 16
        if block_size >= 4096:
            num_warps = 16
        elif block_size >= 2048:
            num_warps = 8
        else:
            num_warps = 4

        if num_rows <= 350000: 
            grid = (num_rows,)
            softmax_kernel[grid](output, input, input.stride(0), num_cols, mask, BLOCK_SIZE = block_size, num_warps=num_warps)
        else:
            grid = lambda meta: ()

            grid = lambda meta: (
                triton.cdiv(num_rows, meta["BLOCK_M"]),
            )

            BLOCK_M = 32
            if block_size >= 4096:
                BLOCK_M = 4
            elif block_size >= 2048:
                BLOCK_M = 8

            softmax_kernel_2[grid](output_ptr = output, 
                            input_ptr = input, 
                            row_stride = input.stride(0), 
                            n_rows = num_rows, 
                            n_cols = num_cols, 
                            mask_ptr = mask, 
                            # currently manually setting up size
                            BLOCK_M = 32, 
                            BLOCK_SIZE = block_size)

        return output