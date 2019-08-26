""" Optimizers class """
import torch
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_
import operator
import functools
from copy import copy
from math import sqrt, cos, pi


def build_torch_optimizer(model, opt):
    """Builds the PyTorch optimizer.

    We use the default parameters for Adam that are suggested by
    the original paper https://arxiv.org/pdf/1412.6980.pdf
    These values are also used by other established implementations,
    e.g. https://www.tensorflow.org/api_docs/python/tf/train/AdamOptimizer
    https://keras.io/optimizers/
    Recently there are slightly different values used in the paper
    "Attention is all you need"
    https://arxiv.org/pdf/1706.03762.pdf, particularly the value beta2=0.98
    was used there however, beta2=0.999 is still arguably the more
    established value, so we use that here as well

    Args:
      model: The model to optimize.
      opt. The dictionary of options.

    Returns:
      A ``torch.optim.Optimizer`` instance.
    """
    params = [p for p in model.parameters() if p.requires_grad]
    betas = [opt.adam_beta1, opt.adam_beta2]
    if opt.optim == 'sgd':
        optimizer = optim.SGD(params, lr=opt.learning_rate)
    elif opt.optim == 'adagrad':
        optimizer = optim.Adagrad(
            params,
            lr=opt.learning_rate,
            initial_accumulator_value=opt.adagrad_accumulator_init)
    elif opt.optim == 'adadelta':
        optimizer = optim.Adadelta(params, lr=opt.learning_rate)
    elif opt.optim == 'adafactor':
        optimizer = AdaFactor(
            params,
            non_constant_decay=True,
            enable_factorization=True,
            weight_decay=0)
    elif opt.optim == 'adam':
        optimizer = optim.Adam(
            params,
            lr=opt.learning_rate,
            betas=betas,
            eps=1e-9)
    elif opt.optim == 'bertadam':
        optimizer = BertAdam(
            params,
            lr=opt.learning_rate,
            betas=betas,
            eps=1e-9)
    elif opt.optim == 'sparseadam':
        dense = []
        sparse = []
        for name, param in model.named_parameters():
            if not param.requires_grad:
                continue
            # TODO: Find a better way to check for sparse gradients.
            if 'embed' in name:
                sparse.append(param)
            else:
                dense.append(param)
        optimizer = MultipleOptimizer(
            [optim.Adam(
                dense,
                lr=opt.learning_rate,
                betas=betas,
                eps=1e-8),
             optim.SparseAdam(
                 sparse,
                 lr=opt.learning_rate,
                 betas=betas,
                 eps=1e-8)])
    elif opt.optim == 'fusedadam':
        import apex
        optimizer = apex.optimizers.FusedAdam(
            params,
            lr=opt.learning_rate,
            betas=betas)
    else:
        raise ValueError('Invalid optimizer type: ' + opt.optim)

    if opt.model_dtype == 'fp16':
        import apex
        loss_scale = "dynamic" if opt.loss_scale == 0 else opt.loss_scale
        model, optimizer = apex.amp.initialize(
            [model, model.generator],
            optimizer,
            opt_level=opt.apex_opt_level,
            loss_scale=loss_scale,
            keep_batchnorm_fp32=False if opt.optim == "fusedadam" else None)

    return optimizer


def make_learning_rate_decay_fn(opt):
    """Returns the learning decay function from options."""
    if opt.decay_method == 'noam':
        return functools.partial(
            noam_decay,
            warmup_steps=opt.warmup_steps,
            model_size=opt.rnn_size)
    elif opt.decay_method == 'noamwd':
        return functools.partial(
            noamwd_decay,
            warmup_steps=opt.warmup_steps,
            model_size=opt.rnn_size,
            rate=opt.learning_rate_decay,
            decay_steps=opt.decay_steps,
            start_step=opt.start_decay_steps)
    elif opt.decay_method == 'linear':
        return functools.partial(
            linear_decay,
            warmup_steps=opt.warmup_steps,
            total_steps=opt.train_steps)
    elif opt.decay_method == 'linearconst':
        return functools.partial(
            linear_decay,
            warmup_steps=opt.warmup_steps)
    elif opt.decay_method == 'cosine':
        return functools.partial(
            cosine_decay,
            warmup_steps=opt.warmup_steps,
            total_steps=opt.train_steps,
            cycles=opt.cycles if opt.cycles is not None else 0.5)
    elif opt.decay_method == 'cosine_hard_restart':
        return functools.partial(
            cosine_hard_restart_decay,
            warmup_steps=opt.warmup_steps,
            total_steps=opt.train_steps,
            cycles=opt.cycles if opt.cycles is not None else 1.0)
    elif opt.decay_method == 'cosine_warmup_restart':
        return functools.partial(
            cosine_warmup_restart_decay,
            warmup_steps=opt.warmup_steps,
            total_steps=opt.train_steps,
            cycles=opt.cycles if opt.cycles is not None else 1.0)
    elif opt.decay_method == 'rsqrt':
        return functools.partial(
            rsqrt_decay, warmup_steps=opt.warmup_steps)
    elif opt.start_decay_steps is not None:
        return functools.partial(
            exponential_decay,
            rate=opt.learning_rate_decay,
            decay_steps=opt.decay_steps,
            start_step=opt.start_decay_steps)


def noam_decay(step, warmup_steps, model_size):
    """Learning rate schedule described in
    https://arxiv.org/pdf/1706.03762.pdf.
    """
    return (
        model_size ** (-0.5) *
        min(step ** (-0.5), step * warmup_steps**(-1.5)))


def noamwd_decay(step, warmup_steps,
                 model_size, rate, decay_steps, start_step=0):
    """Learning rate schedule optimized for huge batches
    """
    return (
        model_size ** (-0.5) *
        min(step ** (-0.5), step * warmup_steps**(-1.5)) *
        rate ** (max(step - start_step + decay_steps, 0) // decay_steps))


def exponential_decay(step, rate, decay_steps, start_step=0):
    """A standard exponential decay, scaling the learning rate by :obj:`rate`
    every :obj:`decay_steps` steps.
    """
    return rate ** (max(step - start_step + decay_steps, 0) // decay_steps)


def rsqrt_decay(step, warmup_steps):
    """Decay based on the reciprocal of the step square root."""
    return 1.0 / sqrt(max(step, warmup_steps))


def linear_decay(step, warmup_steps, total_steps):
    """Linearly increase the lr from 0 to 1 over (0, warmup_steps),
    Then, linearly decrease the lr from 1 to 0 over (warmup_steps, train_step)
    """
    if not 0 <= warmup_steps < total_steps:
        raise ValueError("Invalid decay: check warmup_step & train_steps")
    if step < warmup_steps:
        return step / warmup_steps * 1.0
    else:
        return max((total_steps - step) / (total_steps - warmup_steps), 0)


def linear_constant_decay(step, warmup_steps):
    """Linearly increase the lr from 0 to 1 over (0, warmup_steps),
    Then, keep constant.
    """
    if step < warmup_steps:
        return step / warmup_steps * 1.0
    return 1.0


def cosine_decay(step, warmup_steps, total_steps, cycles=0.5):
    """Linearly increase the lr from 0 to 1 over (0, warmup_steps),
    Then, decrease lr from 1 to 0 over (warmup_steps, train_step)
    following cosine curve.
    """

    if not 0 <= warmup_steps < total_steps:
        raise ValueError("Invalid decay: check warmup_step & train_steps")
    if step < warmup_steps:
        return step / warmup_steps * 1.0
    else:
        progress = (step - warmup_steps) / (total_steps - warmup_steps)
        return 0.5 * (1 + cos(pi * cycles * 2 * progress))


def cosine_hard_restart_decay(step, warmup_steps, total_steps, cycles=1.0):
    """Linearly increase the lr from 0 to 1 over (0, warmup_steps),
    Then, decrease the lr from 1 over (warmup_steps, train_step)
    following cosine curve.
    If `cycles` is different from default(1.0), learning rate follows
    `cycles` times a cosine decaying learning rate (with hard restarts).
    """
    assert(cycles >= 1.0)
    if not 0 <= warmup_steps < total_steps:
        raise ValueError("Invalid decay: check warmup_step & train_steps")
    if step < warmup_steps:
        return step / warmup_steps * 1.0
    else:
        progress = (step - warmup_steps) / (total_steps - warmup_steps)
        return 0.5 * (1 + cos(pi * ((cycles * progress) % 1)))


def cosine_warmup_restart_decay(step, warmup_steps, total_steps, cycles=1.0):
    """Linearly increase the lr from 0 to 1 over (0, warmup_steps),
    Then, decrease the lr from 1 to 0 over (warmup_steps, train_step)
    following cosine curve.
    """
    if not 0 <= warmup_steps < total_steps:
        raise ValueError("Invalid decay: check warmup_step & train_steps")
    if not cycles * warmup_steps / total_steps < 1.0:
        raise ValueError("Invalid decay: Error for decay! Check cycles!")
    warmup_ratio = warmup_steps * cycles / total_steps
    progress = (step * cycles / total_steps) % 1
    if progress < warmup_ratio:
        return progress / warmup_ratio
    else:
        progress = (progress - warmup_ratio) / (1 - warmup_ratio)
        return 0.5 * (1 + cos(pi * progress))


class MultipleOptimizer(object):
    """ Implement multiple optimizers needed for sparse adam """

    def __init__(self, op):
        """ ? """
        self.optimizers = op

    @property
    def param_groups(self):
        param_groups = []
        for optimizer in self.optimizers:
            param_groups.extend(optimizer.param_groups)
        return param_groups

    def zero_grad(self):
        """ ? """
        for op in self.optimizers:
            op.zero_grad()

    def step(self):
        """ ? """
        for op in self.optimizers:
            op.step()

    @property
    def state(self):
        """ ? """
        return {k: v for op in self.optimizers for k, v in op.state.items()}

    def state_dict(self):
        """ ? """
        return [op.state_dict() for op in self.optimizers]

    def load_state_dict(self, state_dicts):
        """ ? """
        assert len(state_dicts) == len(self.optimizers)
        for i in range(len(state_dicts)):
            self.optimizers[i].load_state_dict(state_dicts[i])


class Optimizer(object):
    """
    Controller class for optimization. Mostly a thin
    wrapper for `optim`, but also useful for implementing
    rate scheduling beyond what is currently available.
    Also implements necessary methods for training RNNs such
    as grad manipulations.
    """

    def __init__(self,
                 optimizer,
                 learning_rate,
                 learning_rate_decay_fn=None,
                 max_grad_norm=None):
        """Initializes the controller.

       Args:
         optimizer: A ``torch.optim.Optimizer`` instance.
         learning_rate: The initial learning rate.
         learning_rate_decay_fn: An optional callable taking the current step
           as argument and return a learning rate scaling factor.
         max_grad_norm: Clip gradients to this global norm.
        """
        self._optimizer = optimizer
        self._learning_rate = learning_rate
        self._learning_rate_decay_fn = learning_rate_decay_fn
        self._max_grad_norm = max_grad_norm or 0
        self._training_step = 1
        self._decay_step = 1
        self._with_fp16_wrapper = (
            optimizer.__class__.__name__ == "FP16_Optimizer")

    @classmethod
    def from_opt(cls, model, opt, checkpoint=None):
        """Builds the optimizer from options.

        Args:
          cls: The ``Optimizer`` class to instantiate.
          model: The model to optimize.
          opt: The dict of user options.
          checkpoint: An optional checkpoint to load states from.

        Returns:
          An ``Optimizer`` instance.
        """
        optim_opt = opt
        optim_state_dict = None

        if opt.train_from and checkpoint is not None \
           and 'optim' in checkpoint:
            optim = checkpoint['optim']
            ckpt_opt = checkpoint['opt']
            ckpt_state_dict = {}
            if isinstance(optim, Optimizer):  # Backward compatibility.
                ckpt_state_dict['training_step'] = optim._step + 1
                ckpt_state_dict['decay_step'] = optim._step + 1
                ckpt_state_dict['optimizer'] = optim.optimizer.state_dict()
            else:
                ckpt_state_dict = optim

            if opt.reset_optim == 'none':
                # Load everything from the checkpoint.
                optim_opt = ckpt_opt
                optim_state_dict = ckpt_state_dict
            elif opt.reset_optim == 'all':
                # Build everything from scratch.
                pass
            elif opt.reset_optim == 'states':
                # Reset optimizer, keep options.
                optim_opt = ckpt_opt
                optim_state_dict = ckpt_state_dict
                del optim_state_dict['optimizer']
            elif opt.reset_optim == 'keep_states':
                # Reset options, keep optimizer.
                optim_state_dict = ckpt_state_dict

        optimizer = cls(
            build_torch_optimizer(model, optim_opt),
            optim_opt.learning_rate,
            learning_rate_decay_fn=make_learning_rate_decay_fn(optim_opt),
            max_grad_norm=optim_opt.max_grad_norm)
        if optim_state_dict:
            optimizer.load_state_dict(optim_state_dict)
        return optimizer

    @property
    def training_step(self):
        """The current training step."""
        return self._training_step

    def learning_rate(self):
        """Returns the current learning rate."""
        if self._learning_rate_decay_fn is None:
            return self._learning_rate
        scale = self._learning_rate_decay_fn(self._decay_step)
        return scale * self._learning_rate

    def state_dict(self):
        return {
            'training_step': self._training_step,
            'decay_step': self._decay_step,
            'optimizer': self._optimizer.state_dict()
        }

    def load_state_dict(self, state_dict):
        self._training_step = state_dict['training_step']
        # State can be partially restored.
        if 'decay_step' in state_dict:
            self._decay_step = state_dict['decay_step']
        if 'optimizer' in state_dict:
            self._optimizer.load_state_dict(state_dict['optimizer'])

    def zero_grad(self):
        """Zero the gradients of optimized parameters."""
        self._optimizer.zero_grad()

    def backward(self, loss):
        """Wrapper for backward pass. Some optimizer requires ownership of the
        backward pass."""
        if self._with_fp16_wrapper:
            import apex
            with apex.amp.scale_loss(loss, self._optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

    def step(self):
        """Update the model parameters based on current gradients.

        Optionally, will employ gradient modification or update learning
        rate.
        """
        learning_rate = self.learning_rate()
        if self._with_fp16_wrapper:
            if hasattr(self._optimizer, "update_master_grads"):
                self._optimizer.update_master_grads()
            if hasattr(self._optimizer, "clip_master_grads") and \
               self._max_grad_norm > 0:
                import apex
                torch.nn.utils.glip_grad_norm_(
                    apex.amp.master_params(self), self._max_grad_norm)
        for group in self._optimizer.param_groups:
            group['lr'] = learning_rate
            if not self._with_fp16_wrapper and self._max_grad_norm > 0:
                clip_grad_norm_(group['params'], self._max_grad_norm)
        self._optimizer.step()
        self._decay_step += 1
        self._training_step += 1

# Code below is an implementation of https://arxiv.org/pdf/1804.04235.pdf
# inspired but modified from https://github.com/DeadAt0m/adafactor-pytorch


class AdaFactor(torch.optim.Optimizer):

    def __init__(self, params, lr=None, beta1=0.9, beta2=0.999, eps1=1e-30,
                 eps2=1e-3, cliping_threshold=1, non_constant_decay=True,
                 enable_factorization=True, ams_grad=True, weight_decay=0):

        enable_momentum = beta1 != 0

        if non_constant_decay:
            ams_grad = False

        defaults = dict(lr=lr, beta1=beta1, beta2=beta2, eps1=eps1,
                        eps2=eps2, cliping_threshold=cliping_threshold,
                        weight_decay=weight_decay, ams_grad=ams_grad,
                        enable_factorization=enable_factorization,
                        enable_momentum=enable_momentum,
                        non_constant_decay=non_constant_decay)

        super(AdaFactor, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(AdaFactor, self).__setstate__(state)

    def _experimental_reshape(self, shape):
        temp_shape = shape[2:]
        if len(temp_shape) == 1:
            new_shape = (shape[0], shape[1]*shape[2])
        else:
            tmp_div = len(temp_shape) // 2 + len(temp_shape) % 2
            new_shape = (shape[0]*functools.reduce(operator.mul,
                                                   temp_shape[tmp_div:], 1),
                         shape[1]*functools.reduce(operator.mul,
                                                   temp_shape[:tmp_div], 1))
        return new_shape, copy(shape)

    def _check_shape(self, shape):
        '''
        output1 - True - algorithm for matrix, False - vector;
        output2 - need reshape
        '''
        if len(shape) > 2:
            return True, True
        elif len(shape) == 2:
            return True, False
        elif len(shape) == 2 and (shape[0] == 1 or shape[1] == 1):
            return False, False
        else:
            return False, False

    def _rms(self, x):
        return sqrt(torch.mean(x.pow(2)))

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data

                if grad.is_sparse:
                    raise RuntimeError('Adam does not support sparse \
                                       gradients, use SparseAdam instead')

                is_matrix, is_need_reshape = self._check_shape(grad.size())
                new_shape = p.data.size()
                if is_need_reshape and group['enable_factorization']:
                    new_shape, old_shape = \
                        self._experimental_reshape(p.data.size())
                    grad = grad.view(new_shape)

                state = self.state[p]
                if len(state) == 0:
                    state['step'] = 0
                    if group['enable_momentum']:
                        state['exp_avg'] = torch.zeros(new_shape,
                                                       dtype=torch.float32,
                                                       device=p.grad.device)

                    if is_matrix and group['enable_factorization']:
                        state['exp_avg_sq_R'] = \
                            torch.zeros((1, new_shape[1]),
                                        dtype=torch.float32,
                                        device=p.grad.device)
                        state['exp_avg_sq_C'] = \
                            torch.zeros((new_shape[0], 1),
                                        dtype=torch.float32,
                                        device=p.grad.device)
                    else:
                        state['exp_avg_sq'] = torch.zeros(new_shape,
                                                          dtype=torch.float32,
                                                          device=p.grad.device)
                    if group['ams_grad']:
                        state['exp_avg_sq_hat'] = \
                            torch.zeros(new_shape, dtype=torch.float32,
                                        device=p.grad.device)

                if group['enable_momentum']:
                    exp_avg = state['exp_avg']

                if is_matrix and group['enable_factorization']:
                    exp_avg_sq_r = state['exp_avg_sq_R']
                    exp_avg_sq_c = state['exp_avg_sq_C']
                else:
                    exp_avg_sq = state['exp_avg_sq']

                if group['ams_grad']:
                    exp_avg_sq_hat = state['exp_avg_sq_hat']

                state['step'] += 1
                lr_t = group['lr']
                lr_t *= max(group['eps2'], self._rms(p.data))

                if group['enable_momentum']:
                    if group['non_constant_decay']:
                        beta1_t = group['beta1'] * \
                                  (1 - group['beta1'] ** (state['step'] - 1)) \
                                  / (1 - group['beta1'] ** state['step'])
                    else:
                        beta1_t = group['beta1']
                    exp_avg.mul_(beta1_t).add_(1 - beta1_t, grad)

                if group['non_constant_decay']:
                    beta2_t = group['beta2'] * \
                              (1 - group['beta2'] ** (state['step'] - 1)) / \
                              (1 - group['beta2'] ** state['step'])
                else:
                    beta2_t = group['beta2']

                if is_matrix and group['enable_factorization']:
                    exp_avg_sq_r.mul_(beta2_t). \
                        add_(1 - beta2_t, torch.sum(torch.mul(grad, grad).
                                                    add_(group['eps1']),
                                                    dim=0, keepdim=True))
                    exp_avg_sq_c.mul_(beta2_t). \
                        add_(1 - beta2_t, torch.sum(torch.mul(grad, grad).
                                                    add_(group['eps1']),
                                                    dim=1, keepdim=True))
                    v = torch.mul(exp_avg_sq_c,
                                  exp_avg_sq_r).div_(torch.sum(exp_avg_sq_r))
                else:
                    exp_avg_sq.mul_(beta2_t). \
                        addcmul_(1 - beta2_t, grad, grad). \
                        add_((1 - beta2_t)*group['eps1'])
                    v = exp_avg_sq

                g = grad
                if group['enable_momentum']:
                    g = torch.div(exp_avg, 1 - beta1_t ** state['step'])

                if group['ams_grad']:
                    torch.max(exp_avg_sq_hat, v, out=exp_avg_sq_hat)
                    v = exp_avg_sq_hat
                    u = torch.div(g, (torch.div(v, 1 - beta2_t **
                                  state['step'])).sqrt().add_(group['eps1']))
                else:
                    u = torch.div(g, v.sqrt())

                u.div_(max(1, self._rms(u) / group['cliping_threshold']))
                p.data.add_(-lr_t * (u.view(old_shape) if is_need_reshape and
                            group['enable_factorization'] else u))

                if group['weight_decay'] != 0:
                    p.data.add_(-group['weight_decay'] * lr_t, p.data)

        return loss


class BertAdam(torch.optim.Optimizer):
    """Implements Adam algorithm with weight decay fix
       (used in BERT while doesn't compensate for bias).
        :cite:`DBLP:journals/corr/abs-1711-05101`

    Args:
        lr (float): learning rate
        betas (tuple of float): Adam (beta1, beta2). Default: (0.9, 0.999)
        eps (float): Adams epsilon. Default: 1e-6
        weight_decay (float): Weight decay. Default: 0.01
        max_grad_norm (float): -1 means no gradients clipping.
    """

    def __init__(self, params, lr=None, betas=(0.9, 0.999),
                 eps=1e-6, weight_decay=0.01, max_grad_norm=1.0, **kwargs):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr) +
                             " - should be >= 0.0")
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid betas[0] parameter: {}".format(
                             betas[0]) + " - should be in [0.0, 1.0)")
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid betas[1] parameter: {}".format(
                             betas[1]) + " - should be in [0.0, 1.0)")
        if not eps >= 0.0:
            raise ValueError("Invalid epsilon value: {}".format(eps) +
                             " - should be >= 0.0")
        defaults = dict(lr=lr, betas=betas,
                        eps=eps, weight_decay=weight_decay,
                        max_grad_norm=max_grad_norm)
        super(BertAdam, self).__init__(params, defaults)

    def step(self, closure=None):
        """Performs a single optimization step.

        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data
                if grad.is_sparse:
                    raise RuntimeError('Adam: not support sparse gradients,' +
                                       'please consider SparseAdam instead')

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    # state['step'] = 0
                    # Exponential moving average of gradient values
                    state['exp_avg'] = torch.zeros_like(p.data)
                    # Exponential moving average of squared gradient values
                    state['exp_avg_sq'] = torch.zeros_like(p.data)

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                beta1, beta2 = group['betas']

                # NOTE: Add grad clipping, DONE before step function
                # if group['max_grad_norm'] > 0:
                #     clip_grad_norm_(p, group['max_grad_norm'])

                # Decay first and second moment running average coefficient
                # In-place operations to update the averages at the same time
                # exp_avg = exp_avg * beta1 + (1-beta1)*grad
                exp_avg.mul_(beta1).add_(1 - beta1, grad)
                # exp_avg_sq = exp_avg_sq * beta2 + (1 - beta2)*grad**2
                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                update = exp_avg / (exp_avg_sq.sqrt() + group['eps'])

                # Ref: https://arxiv.org/abs/1711.05101
                # Just adding the square of the weights to the loss function
                # is *not* the correct way of using L2/weight decay with Adam,
                # since it will interact with m/v parameters in strange ways.
                #
                # Instead we want to decay the weights that does not interact
                # with the m/v. This is equivalent to add the square
                # of the weights to the loss with plain (non-momentum) SGD.
                if group['weight_decay'] > 0.0:
                    update += group['weight_decay'] * p.data

                lr_scheduled = group['lr']

                update_with_lr = lr_scheduled * update
                p.data.add_(-update_with_lr)

                # state['step'] += 1

                # NOTE: BertAdam "No bias correction" comparing to standard
                # bias_correction1 = 1 - betas[0] ** state['step']
                # bias_correction2 = 1 - betas[1] ** state['step']
                # step_size = lr_scheduled * math.sqrt(bias_correction2)
                #              / bias_correction1

        return loss
