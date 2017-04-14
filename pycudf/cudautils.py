from itertools import product
from functools import partial

import numpy as np

from numba import cuda, vectorize, types, uint64, int32, float64, numpy_support

from .utils import mask_bitsize


def to_device(ary):
    dary, _ = cuda._auto_device(ary)
    return dary


# GPU array type casting


@cuda.jit
def gpu_copy(inp, out):
    tid = cuda.grid(1)
    if tid < out.size:
        out[tid] = inp[tid]


def astype(ary, dtype):
    if ary.dtype == np.dtype(dtype):
        return ary
    else:
        out = cuda.device_array(shape=ary.shape, dtype=dtype)
        configured = gpu_copy.forall(out.size)
        configured(ary, out)
        return out


def copy_array(arr, out=None):
    if out is None:
        out = cuda.device_array_like(arr)
    assert out.size == arr.size
    out.copy_to_device(arr)
    return out


# Copy column into a matrix

@cuda.jit
def gpu_copy_column(matrix, colidx, colvals):
    tid = cuda.grid(1)
    if tid < colvals.size:
        matrix[colidx, tid] = colvals[tid]


def copy_column(matrix, colidx, colvals):
    assert matrix.shape[1] == colvals.size
    configured = gpu_copy_column.forall(colvals.size)
    configured(matrix, colidx, colvals)


# Mask utils

@cuda.jit
def gpu_set_mask_from_stride(mask, stride):
    bitsize = mask_bitsize
    tid = cuda.grid(1)
    if tid < mask.size:
        base = tid * bitsize
        val = 0
        for shift in range(bitsize):
            idx = base + shift
            bit = (idx % stride) == 0
            val |= bit << shift
        mask[tid] = val


def set_mask_from_stride(mask, stride):
    taskct = mask.size
    configured = gpu_set_mask_from_stride.forall(taskct)
    configured(mask, stride)


@cuda.jit(device=True)
def gpu_mask_get(mask, pos):
    return (mask[pos // mask_bitsize] >> (pos % mask_bitsize)) & 1


@cuda.jit(device=True)
def gpu_prefixsum(data, initial):
    # XXX slow imp
    acc = initial
    for i in range(data.size):
        tmp = data[i]
        data[i] = acc
        acc += tmp
    return acc


@cuda.jit
def gpu_mask_assign_slot(mask, slots):
    """
    Assign output slot from the given mask.

    Note:
    * this is a single wrap, single block kernel
    """
    tid = cuda.threadIdx.x
    blksz = cuda.blockDim.x

    sm_prefix = cuda.shared.array(shape=(33,), dtype=uint64)
    offset = 0
    for base in range(0, slots.size, blksz):
        i = base + tid
        sm_prefix[tid] = 0
        if i < slots.size:
            sm_prefix[tid] = gpu_mask_get(mask, i)
        if tid == 0:
            offset = gpu_prefixsum(sm_prefix[:32], offset)
            sm_prefix[32] = offset
        offset = sm_prefix[32]
        if i < slots.size:
            slots[i] = sm_prefix[tid]


@cuda.jit
def gpu_copy_to_dense(data, mask, slots, out):
    tid = cuda.grid(1)
    if tid < data.size and gpu_mask_get(mask, tid):
        idx = slots[tid]
        out[idx] = data[tid]


def copy_to_dense(data, mask, out=None):
    """
    The output array can be specified in `out`.

    Return a 2-tuple of:
    * number of non-null element
    * a dense gpu array given the data and mask gpu arrays.
    """
    slots = cuda.device_array(shape=data.shape, dtype=np.uint64)
    gpu_mask_assign_slot[1, 32](mask, slots)
    sz = slots[-1:].copy_to_host() + 1
    if out is None:
        out = cuda.device_array(shape=sz, dtype=data.dtype)
    else:
        # check
        if sz >= out.size:
            raise ValueError('output array too small')
    gpu_copy_to_dense.forall(data.size)(data, mask, slots, out)
    return (sz, out)


#
# Fill NA
#


@cuda.jit
def gpu_fill_masked(value, validity, out):
    tid = cuda.grid(1)
    if tid < out.size:
        valid = gpu_mask_get(validity, tid)
        if not valid:
            out[tid] = value


def fillna(data, mask, value):
    out = cuda.device_array_like(data)
    out.copy_to_device(data)
    configured = gpu_fill_masked.forall(data.size)
    configured(value, mask, out)
    return out


#
# Binary kernels
#

@cuda.jit
def gpu_equal_constant(arr, val, out):
    i = cuda.grid(1)
    if i < out.size:
        out[i] = (arr[i] == val)


def apply_equal_constant(arr, val, dtype):
    out = cuda.device_array(shape=arr.size, dtype=dtype)
    configured = gpu_equal_constant.forall(out.size)
    configured(arr, val, out)
    return out


#
# Reduction kernels
#

gpu_sum = cuda.reduce(lambda x, y: x + y)
gpu_min = cuda.reduce(lambda x, y: min(x, y))
gpu_max = cuda.reduce(lambda x, y: max(x, y))


def _run_reduction(gpu_reduce, arr, init=0, inplace=False):
    """
    If *inplace* is True, the *arr* is overwritten by this function.
    """
    if not inplace:
        arr = copy_array(arr)
    return gpu_reduce(arr, init=init)


compute_sum = partial(_run_reduction, gpu_sum, init=0)
compute_min = partial(_run_reduction, gpu_min)
compute_max = partial(_run_reduction, gpu_max)


def compute_mean(arr, inplace=False):
    """
    If *inplace* is True, the *arr* is overwritten by this function.
    """
    return compute_sum(arr, inplace=inplace) / arr.size


@cuda.jit
def gpu_variance_step(xs, mu, out):
    tid = cuda.grid(1)
    if tid < out.size:
        x = xs[tid]
        out[tid] = (x - mu) ** 2


def compute_stats(arr):
    """
    Returns (mean, variance)
    """
    mu = compute_mean(arr)
    tmp = cuda.device_array_like(arr)
    gpu_variance_step.forall(arr.size)(arr, mu, tmp)
    return mu, compute_mean(tmp, inplace=True)


@cuda.jit(device=True)
def gpu_unique_set_insert(vset, sz, val):
    """
    Insert *val* into the *vset* of size *sz*.
    Returns:
        *   -1: out-of-space
        * >= 0: the new size
    """
    for i in range(sz):
        # value matches, so return current size
        if vset[i] == val:
            return sz

    if sz > 0:
        i += 1
    # insert at next available slot
    if i < vset.size:
        vset[i] = val
        return sz + 1

    # out of space
    return -1


MAX_FAST_UNIQUE_K = 2 * 1024


class UniqueK(object):
    _cached_kernels = {}

    def __init__(self, dtype):
        dtype = np.dtype(dtype)
        self._kernel = self._get_kernel(dtype)

    @classmethod
    def _get_kernel(cls, dtype):
        try:
            return cls._cached_kernels[dtype]
        except KeyError:
            return cls._compile(dtype)

    @classmethod
    def _compile(cls, dtype):
        nbtype = numpy_support.from_dtype(dtype)

        @cuda.jit
        def gpu_unique_k(arr, k, out, outsz_ptr):
            """
            Note: run with small blocks.
            """
            tid = cuda.threadIdx.x
            blksz = cuda.blockDim.x
            base = 0

            # shared memory
            vset_size = 0
            sm_mem_size = MAX_FAST_UNIQUE_K
            vset = cuda.shared.array(sm_mem_size, dtype=nbtype)
            share_vset_size = cuda.shared.array(1, dtype=int32)
            share_loaded = cuda.shared.array(sm_mem_size, dtype=nbtype)
            sm_mem_size = min(k, sm_mem_size)

            while vset_size < sm_mem_size and base < arr.size:
                pos = base + tid
                valid_load = min(blksz, arr.size - base)
                # load
                if tid < valid_load:
                    share_loaded[tid] = arr[pos]
                # wait for load to complete
                cuda.syncthreads()
                # thread-0 inserts
                if tid == 0:
                    for i in range(valid_load):
                        val = share_loaded[i]
                        new_size = gpu_unique_set_insert(vset, vset_size, val)
                        if new_size >= 0:
                            vset_size = new_size
                        else:
                            vset_size = sm_mem_size + 1
                    share_vset_size[0] = vset_size
                # wait until the insert is done
                cuda.syncthreads()
                vset_size = share_vset_size[0]
                # increment
                base += blksz

            # output
            if vset_size <= sm_mem_size:
                for i in range(tid, vset_size, blksz):
                    out[i] = vset[i]
                if tid == 0:
                    outsz_ptr[0] = vset_size
            else:
                outsz_ptr[0] = -1

        # cache
        cls._cached_kernels[dtype] = gpu_unique_k
        return gpu_unique_k

    def run(self, arr, k):
        if k >= MAX_FAST_UNIQUE_K:
            raise NotImplementedError('k >= {}'.format(MAX_FAST_UNIQUE_K))
        # setup mem
        outsz_ptr = cuda.device_array(shape=1, dtype=np.intp)
        out = cuda.device_array_like(arr)
        # kernel
        self._kernel[1, 64](arr, k, out, outsz_ptr)
        # copy to host
        unique_ct = outsz_ptr.copy_to_host()[0]
        if unique_ct < 0:
            raise ValueError('too many unique value (hint: increase k)')
        else:
            hout = out.copy_to_host()
            return hout[:unique_ct]


def compute_unique_k(arr, k):
    return UniqueK(arr.dtype).run(arr, k)
