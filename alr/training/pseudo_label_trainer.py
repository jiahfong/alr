import torch
from ignite.engine import Engine, Events
from torch import nn
import torch.nn.functional as F
from typing import Optional
from ignite.engine import Engine, Events, \
    create_supervised_evaluator
from ignite.metrics import Loss, Accuracy
import torch.utils.data as torchdata
from ignite.contrib.handlers import ProgressBar
import numpy as np

from collections import defaultdict
from typing import Optional, Dict, Sequence, Union


from alr.training import Trainer
from alr.utils import _DeviceType
from alr.training.utils import EarlyStopper


def soft_nll_loss(preds: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    # -1/N * sum_y p(y)log[p(y)]
    res = -(target.exp() * preds).sum(dim=1).mean()
    assert torch.isfinite(res)
    return res


def soft_cross_entropy(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    # -1/N * sum_y p(y)log[p(y)]
    res = -(F.softmax(target, dim=-1) * F.log_softmax(logits, dim=-1)).sum(dim=1).mean()
    assert torch.isfinite(res)
    return res

r"""
todo(harry):
    2. tracking quality/uncertainty
    3. thresholding capabilities
"""
def create_semisupervised_trainer(model: nn.Module, optimizer,
                                  uloss_fn, lloss_fn, annealer,
                                  train_iterable, soft,
                                  device):
    def _step(_, batch):
        x = batch.to(device)
        # get pseudo-labels for this batch using eval mode
        with torch.no_grad():
            model.eval()
            preds = model(x)
            if not soft:
                preds = torch.argmax(preds, dim=1)

        # normal forward pass on pseudo_labels
        model.train()
        u_loss = uloss_fn(model(x), preds)

        # normal forward pass on training data
        model.train()
        x, y = next(train_iterable)
        x, y = x.to(device), y.to(device)
        l_loss = lloss_fn(model(x), y)

        # total loss
        loss = l_loss + annealer.weight * u_loss
        assert torch.isfinite(loss)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        return loss.item()
    return Engine(_step)


class WraparoundLoader:
    def __init__(self, ds: torchdata.DataLoader):
        self._ds = ds
        self._iter = iter(ds)

    def __next__(self) -> torch.Tensor:
        try:
            return next(self._iter)
        except StopIteration:
            self._iter = iter(self._ds)
            return next(self._iter)

    def __iter__(self):
        return self


class Annealer:
    def __init__(self, step: Optional[int] = 0,
                 T1: Optional[int] = 100, T2: Optional[int] = 700,
                 alpha: Optional[float] = 3.0):
        self._step = step
        self._T1 = T1
        self._T2 = T2
        self._alpha = alpha

    def step(self):
        self._step += 1

    @property
    def weight(self):
        if self._step < self._T1:
            return 0
        elif self._step > self._T2:
            return self._alpha
        else:
            return ((self._step - self._T1) / (self._T2 - self._T1)) * self._alpha


class PLTrainer:
    def __init__(self, model, labelled_loss, unlabelled_loss,
                 optimiser: str, device: _DeviceType = None,
                 *args, **kwargs):
        self._lloss = labelled_loss
        self._uloss = unlabelled_loss
        self._optim = optimiser
        self._device = device
        self._model = model
        self._args = args
        self._kwargs = kwargs

    def fit(self,
            train_loader: torchdata.DataLoader,
            pool_loader: torchdata.DataLoader,
            val_loader: Optional[torchdata.DataLoader] = None,
            epochs: Union[int, Sequence[int]] = 1,
            soft: Optional[bool] = False,
            patience: Optional[int] = None,
            reload_best: Optional[bool] = False) -> Dict[str, list]:
        if isinstance(epochs, int):
            epochs = (epochs, epochs)
        assert len(epochs) == 2
        epoch1, epoch2 = epochs[0], epochs[1]
        # stage 1
        supervised_trainer = Trainer(
            self._model, self._lloss, self._optim,
            self._device, *self._args, **self._kwargs
        )
        # until convergence
        supervised_history = supervised_trainer.fit(
            train_loader, val_loader,
            epochs=epoch1, patience=patience,
            reload_best=reload_best,
        )

        # stage 2
        pbar = ProgressBar()
        history = defaultdict(list)

        train_evaluator = create_supervised_evaluator(
            self._model, metrics={'acc': Accuracy(), 'loss': Loss(self._lloss)},
            device=self._device
        )
        val_evaluator = create_supervised_evaluator(
            self._model, metrics={'acc': Accuracy(), 'loss': Loss(self._lloss)},
            device=self._device
        )

        pbar.attach(train_evaluator, output_transform=lambda x: {'loss': x})

        def _log_metrics(engine: Engine):
            # train loader - save to history and print metrics
            metrics = train_evaluator.run(train_loader).metrics
            history[f"train_acc"].append(metrics['acc'])
            history[f"train_loss"].append(metrics['loss'])
            pbar.log_message(
                f"epoch {engine.state.epoch}/{engine.state.max_epochs}\n"
                f"\ttrain acc = {metrics['acc']}, train loss = {metrics['loss']}"
            )

            if val_loader is None:
                return  # job done

            # val loader - save to history and print metrics. Also, add handlers to
            # evaluator (e.g. early stopping, model checkpointing that depend on val_acc)
            metrics = val_evaluator.run(val_loader).metrics

            history[f"val_acc"].append(metrics['acc'])
            history[f"val_loss"].append(metrics['loss'])
            pbar.log_message(
                f"\tval acc = {metrics['acc']}, val loss = {metrics['loss']}"
            )

        # use an old optimiser with whatever state it already had
        # since we're simulating a continuation
        annealer = Annealer(step=1, T1=0, T2=700)
        ssl_trainer = create_semisupervised_trainer(
            self._model, supervised_trainer._optim, uloss_fn=self._uloss,
            lloss_fn=self._lloss, annealer=annealer,
            train_iterable=WraparoundLoader(train_loader), soft=soft,
            device=self._device
        )
        if val_loader is not None and patience:
            es = EarlyStopper(self._model, patience=patience, trainer=ssl_trainer, key='acc', mode='max')
            es.attach(val_evaluator)
        # events to add:
        #  1. log_metrics
        #  2. annealer update
        ssl_trainer.add_event_handler(Events.EPOCH_COMPLETED, _log_metrics)
        ssl_trainer.add_event_handler(Events.ITERATION_COMPLETED(every=50), lambda _: annealer.step())

        ssl_trainer.run(
            pool_loader, max_epochs=epoch2,
            seed=np.random.randint(0, 1e6)
        )
        if val_loader is not None and patience and reload_best:
            es.reload_best()

        for k, v in supervised_history.items():
            v.extend(history[k])
        # return combined history
        return supervised_history

    def evaluate(self, data_loader: torchdata.DataLoader) -> dict:
        evaluator = create_supervised_evaluator(
            self._model, metrics={'acc': Accuracy(), 'loss': Loss(self._lloss)},
            device=self._device
        )
        return evaluator.run(data_loader).metrics
