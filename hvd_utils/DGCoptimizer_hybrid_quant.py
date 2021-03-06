from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
from datetime import datetime

from horovod.common import init
from horovod.common import size
from horovod.common import local_size
from horovod.common import rank
from horovod.common import local_rank
from horovod.common import mpi_threads_supported
from horovod.common import check_extension

#check_extension('horovod.torch', 'HOROVOD_WITH_PYTORCH',
#                __file__, 'mpi_lib', '_mpi_lib')

from horovod.torch.mpi_ops import allreduce, allreduce_async, allreduce_, allreduce_async_
from horovod.torch.mpi_ops import allgather, allgather_async, _allgather_async
from horovod.torch.mpi_ops import broadcast, broadcast_async, broadcast_, broadcast_async_
from horovod.torch.mpi_ops import poll, synchronize
import numpy as np
from .pruning import select_bs_top, select_bs_bottom, select_trim_topk_mean, select_trim_lowk_mean, select_topk_mean, select_lowk_mean
import horovod.torch as hvd

import torch


class _DGCOptimizer(torch.optim.Optimizer):
    def __init__(self, params, named_parameters=None, use_gpu=True, momentum=0.9, weight_decay=1e-4, use_allgather=True):
        super(self.__class__, self).__init__(params)

        if named_parameters is not None:
            named_parameters = list(named_parameters)
        else:
            named_parameters = []

        # make sure that named_parameters are tuples
        if any([not isinstance(p, tuple) for p in named_parameters]):
            raise ValueError('named_parameters should be a sequence of '
                             'tuples (name, parameter), usually produced by '
                             'model.named_parameters().')

        self._parameter_names = {v: k for k, v
                                 in sorted(named_parameters)}
        self._use_gpu = use_gpu
        self._use_nesterov = True
        self._momentum = momentum
        self._weight_decay = weight_decay
        self._debug = False
        self._use_allgather = use_allgather ##True
        #self._use_allgather = False##True

        # define U for residue, V for momentum
        if self._use_gpu:
            self._masks = {k: torch.zeros(v.size()).cuda() for k, v
                                     in sorted(named_parameters)}
            self._compressed_idx= {k: torch.zeros(0, dtype=torch.long).cuda() for k, v
                                 in sorted(named_parameters)}
            self._compressed_val = {k: torch.zeros(0).cuda() for k, v
                                 in sorted(named_parameters)}
        else:
            self._masks = {k: torch.zeros(v.size()) for k, v
                                     in sorted(named_parameters)}
        self._compressed_len= {k: torch.zeros(0, dtype=torch.long) for k, v
                                 in sorted(named_parameters)}
        self._mid_dict = {k: 0 for k, v
                                 in sorted(named_parameters)}
        self._v_ref = {k: [] for k, v
                                 in sorted(named_parameters)}

        self._compressed_msg_size = {k: 0 for k, v
                                 in sorted(named_parameters)}

        self._handles = {}
        self._handles_val = {}
        self._grad_accs = []

        self.pruning_time = 0.0
        self.select_time = 0.0
        self.pack_time = 0.0
        self.unpack_time = 0.0
        self.mask_time = 0.0
        self.mom_time = 0.0
        self.allreduce_time = 0.0

        self._mid = 0
        self._sparsity = 0.0
        self._it = 0
        self._plan3 = 4194304
        #self._plan3 = 4194304000
        self._plan2 = 131072
        #self._plan1 = 10240
        self._plan1 = 8192 

        #if size() > 1:
        self._register_hooks()

    def _register_hooks(self):
        for param_group in self.param_groups:
            for p in param_group['params']:
                if p.requires_grad:
                    p_tmp = p.expand_as(p)
                    grad_acc = p_tmp.grad_fn.next_functions[0][0]
                    grad_acc.register_hook(self._make_hook(p))
                    self._grad_accs.append(grad_acc)

    def _make_hook(self, p):
        def hook(*ignore):
            assert p not in self._handles
            assert not p.grad.requires_grad
            name = self._parameter_names.get(p)
            p_size = np.prod(p.size())
            torch.cuda.synchronize()
            begin_time =  time.time()

            if self._use_allgather and p_size > self._plan1:
                torch.cuda.synchronize()
                begin_mom_time =  time.time()

                weight_decay = self._weight_decay #group['weight_decay']
                momentum = self._momentum #group['momentum']
                dampening = 0.0 #group['dampening']
                d_p = p.grad.data
                d_p.div_(hvd.size())
                if weight_decay != 0:
                    d_p.add_(weight_decay, p.data)
                if momentum != 0:
                    param_state = self.state[p]
                    if 'momentum_buffer' not in param_state:
                        buf = param_state['momentum_buffer'] = torch.zeros_like(p.data)
                        buf.mul_(momentum).add_(d_p)
                    else:
                        buf = param_state['momentum_buffer']
                        buf.mul_(momentum).add_(1 - dampening, d_p)
                    #TODO
                if 'residue_buffer' not in param_state:
                    rsd = param_state['residue_buffer'] = torch.zeros_like(p.data)
                    rsd.add_(param_state['momentum_buffer'])
                    if self._use_nesterov:
                        rsd  = rsd.add(momentum, d_p)
                else:
                    rsd = param_state['residue_buffer']
                    rsd.add_(param_state['momentum_buffer'])
                    if self._use_nesterov:
                        rsd  = rsd.add(momentum, d_p)

                torch.cuda.synchronize()
                self.mom_time += time.time() - begin_mom_time

                compressed_val = []
                compressed_idx = []

                torch.cuda.synchronize()
                begin_select_time =  time.time()

                if 'flag' not in param_state:
                    param_state['flag'] = 0
                if 'interval' not in param_state:
                    param_state['interval'] = 10
                it = 0
                sparsity = 0.0

                if p_size > self._plan3:
                    if param_state['flag'] == 1:
                        compressed_val, compressed_idx, it, _, sparsity = \
                            select_bs_top(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 0
                    else:
                        compressed_val, compressed_idx, it, _, sparsity = \
                            select_bs_bottom(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 1
                elif p_size > self._plan2:
                    if param_state['flag'] == 1:
                        compressed_val, compressed_idx = \
                            select_trim_topk_mean(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 0
                    else:
                        compressed_val, compressed_idx = \
                            select_trim_lowk_mean(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 1
                else:
                    if param_state['flag'] == 1:
                        compressed_val, compressed_idx = \
                            select_topk_mean(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 0
                    else:
                        compressed_val, compressed_idx = \
                            select_lowk_mean(param_state['residue_buffer'], 0.001)
                        param_state['flag'] = 1

                assert(len(compressed_idx) > 0)
                torch.cuda.synchronize()
                end_select_time =  time.time()
                self.select_time += end_select_time - begin_select_time
                #if param_state['interval'] == 10:
                #    compressed_val, compressed_idx, it, param_state['mid_store'], sparsity = \
                #            select_top_k_thdv3(param_state['residue_buffer'], 0.001)
                #    param_state['interval'] = 0
                #else:
                #    compressed_val, compressed_idx, sparsity = \
                #            select_top_k_fixthd(param_state['residue_buffer'], param_state['mid_store'])
                #    param_state['interval'] += 1
                #if hvd.rank() == 0:
                #    print(name, p.size())
                #if hvd.rank() == 0 and name == "features.27.weight":
                #if name == "features.27.weight":
                #    torch.save(compressed_val, 'compressed_val' + str(local_rank()))
                #    torch.save(compressed_idx, 'compressed_idx' + str(local_rank()))
                #if hvd.rank() == 0 and name == "features.27.weight":
                #    self._it = it
                #    self._mid = param_state['mid_store']
                #    self._sparsity = sparsity
                #tmp_t = torch.tensor([local_len], dtype=torch.long)
#                tmp_t = torch.tensor([local_len])
                # print("len list, ", global_len_list)
                #local_len = torch.min(global_len_list)
                ##print("local_len, ", local_len)
                #compressed_val = compressed_val[0:local_len]
                #compressed_idx = compressed_idx[0:local_len]

                torch.cuda.synchronize()
                begin_mask_time =  time.time()

                masks_size = self._masks[name].size()
                self._masks[name].zero_()
                self._masks[name] = self._masks[name].view(-1)
                self._masks[name][compressed_idx] = 1.0

                self._masks[name] = 1.0 - self._masks[name]
                self._masks[name] = self._masks[name].view(masks_size)

                if self._debug:
                    self._v_ref[name] = param_state['residue_buffer'] * (1.0 - self._masks[name])
                    allreduce_(self._v_ref[name], average = False)


                if hvd.size() == 1:
                    p.grad.data = param_state['residue_buffer'] * (1.0 - self._masks[name])

                param_state['residue_buffer'].mul_(self._masks[name])
                param_state['momentum_buffer'].mul_(self._masks[name])

                end_mask_time =  time.time()
                self.mask_time += end_mask_time - begin_mask_time

                torch.cuda.synchronize()
                begin_pack_time =  time.time()

                if hvd.size() > 1:
                    if self._use_gpu:
                        if p_size > self._plan3:
                            compressed_msg= torch.cat((\
                                torch.tensor([len(compressed_idx)]).type(torch.cuda.LongTensor),\
                                compressed_idx))
                            handle = _allgather_async(compressed_msg, self._compressed_idx[name], name=name + "idx")
                            self._handles[p] = handle

                            handle = _allgather_async(torch.mean(compressed_val), self._compressed_val[name], name=name + "val")
                            self._handles_val[p] = handle
                        else:
                            self._compressed_msg_size[name] = len(compressed_idx)
                            handle = _allgather_async(compressed_idx, self._compressed_idx[name], \
                                    name = name+"idx")
                            self._handles[p] = handle
                            handle = _allgather_async(torch.mean(compressed_val), \
                                    self._compressed_val[name], name=name+"val")
                            self._handles_val[p] = handle
                torch.cuda.synchronize()
                self.pack_time += time.time() - begin_pack_time
            else:
                torch.cuda.synchronize()
                begin_allreduce_time =  time.time()
                p.grad.data.div_(hvd.size())
                p.grad.data.add_(torch.mul(p.data, self._weight_decay))
                param_state = self.state[p]
                if 'momentum_buffer' not in param_state:
                    param_state['momentum_buffer'] = torch.zeros_like(p.data)
                if self._use_nesterov:
                    param_state['momentum_buffer'] = torch.mul(torch.add(param_state['momentum_buffer'], p.grad.data), self._momentum)
                    p.grad.data = param_state['momentum_buffer'] + p.grad.data
                else:
                    param_state['momentum_buffer']= self._momentum * param_state['momentum_buffer'] + p.grad.data
                    p.grad.data = param_state['momentum_buffer']
                if hvd.size() > 1:
                    handle = allreduce_async_(p.grad.data, average=False, name=name)
                    self._handles[p] = handle
                torch.cuda.synchronize()
                self.allreduce_time += time.time() - begin_allreduce_time

            torch.cuda.synchronize()
            end_time = time.time()
            self.pruning_time += end_time - begin_time

        return hook

    def synchronize(self):
        if hvd.size() > 1:
            for p in self._handles:
                handle = self._handles[p]
                synchronize(handle)
                #p_size = np.prod(p.size())

                p_size = torch.numel(p)
                if self._use_allgather and p_size > self._plan1:
                    handle = self._handles_val[p]
                    synchronize(handle)
                    torch.cuda.synchronize()
                    begin_time_sync = time.time()
                    #fjr decompress
                    name = self._parameter_names.get(p)

                    g_size = p.grad.data.size()
                    p_flatten = p.grad.data.view(-1)
                    p_flatten.zero_()

                    torch.cuda.synchronize()
                    begin_unpack_time =  time.time()
                    if self._use_gpu:
                        if p_size > self._plan3:
                            #count_nnz = 0
                            offset = 0
                            for node_idx in range(hvd.size()):
                                msg_size = self._compressed_idx[name][offset]
                                offset += 1
                                p_flatten[self._compressed_idx[name][ offset: \
                                        offset + msg_size]] += \
                                        self._compressed_val[name][node_idx]
                                offset += msg_size;
                            #count_nnz += msg_size
                            #if hvd.rank() == 0:
                            #    print("sparsity ", name, count_nnz.cpu().numpy()/(p_size))
                        else:
                            msg_size = self._compressed_msg_size[name]
                            for node_idx in range(hvd.size()):
                                p_flatten[self._compressed_idx[name][node_idx*msg_size : \
                                        node_idx*msg_size + msg_size]] += \
                                        self._compressed_val[name][node_idx]

                    p.grad.data = p_flatten.view(g_size)
                    torch.cuda.synchronize()
                    self.unpack_time += time.time() - begin_unpack_time
                    torch.cuda.synchronize()
                    self.pruning_time += time.time() - begin_time_sync

                    if self._debug:
                        diff = torch.sum(self._v_ref[name] - p.grad.data)
                        if( torch.abs(diff) > 1e-3 ):
                            print("error diff is, ", diff, name, p.size())

                else:
                    pass

        self._handles.clear()
        self._handles_val.clear()

    def step(self, closure=None):
        self.synchronize()
        return super(self.__class__, self).step(closure)


def DGCDistributedOptimizer(optimizer, named_parameters=None, use_gpu=True, momentum=0.9, weight_decay=1e-4, use_allgather=True):
    """
    An optimizer that wraps another torch.optim.Optimizer, 
    Compress gradients according to their magnitude
    using an allgather to reduce compressed gradient values before applying gradients to model weights.
    Allreduce operations are executed after each gradient is computed by `loss.backward()`
    in parallel with each other. The `step()` method ensures that all allreduce operations are
    finished before applying gradients to the model.
    DistributedOptimizer exposes the `synchronize()` method, which forces allreduce operations
    to finish before continuing the execution. It's useful in conjunction with gradient
    clipping, or other operations that modify gradients in place before `step()` is executed.
    Example of gradient clipping:
    ```
    output = model(data)
    loss = F.nll_loss(output, target)
    loss.backward()
    optimizer.synchronize()
    torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
    optimizer.step()
    ```
    Arguments:
        optimizer: Optimizer to use for computing gradients and applying updates.
        named_parameters: A mapping between parameter names and values. Used for naming of
                          allreduce operations. Typically just `model.named_parameters()`.
    """
    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override the `step()` method with an allreduce implementation.
    cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
               dict(_DGCOptimizer.__dict__))
    return cls(optimizer.param_groups, named_parameters,use_gpu, momentum, weight_decay, use_allgather)

