from ignite.engine import create_supervised_evaluator

from alr.training.utils import PLPredictionSaver
from alr.utils import eval_fwd_exp, timeop, manual_seed
from alr import MCDropout
from alr.data.datasets import Dataset
from alr.data import UnlabelledDataset, DataManager
from alr.acquisition import BALD
from alr.training.ephemeral_trainer import EphemeralTrainer, Trainer
from alr.training.samplers import RandomFixedLengthSampler

import pickle
from collections import defaultdict
from pathlib import Path
import torch.utils.data as torchdata
import torch
from torch.nn import functional as F


def main(threshold: float, b: int):
    manual_seed(42)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    kwargs = dict(num_workers=4, pin_memory=True)

    BATCH_SIZE = 64
    REPS = 6
    ITERS = 24
    VAL_SIZE = 5_000
    MIN_TRAIN_LEN = 12_500
    SSL_ITERATIONS = 200
    EPOCHS = 200

    accs = defaultdict(list)

    template = f"thresh_{threshold}_b_{b}"
    calib_metrics = Path("calib_metrics") / template
    saved_models = Path("saved_models") / template
    metrics = Path("metrics") / template
    calib_metrics.mkdir(parents=True)
    saved_models.mkdir(parents=True)
    metrics.mkdir(parents=True)

    train, pool, test = Dataset.MNIST.get_fixed()
    val, pool = torchdata.random_split(pool, (VAL_SIZE, len(pool) - VAL_SIZE))
    pool = UnlabelledDataset(pool)
    test_loader = torchdata.DataLoader(test, batch_size=512, shuffle=False, **kwargs)
    val_loader = torchdata.DataLoader(val, batch_size=512, shuffle=False, **kwargs)

    for r in range(1, REPS + 1):
        model = MCDropout(Dataset.MNIST.model, forward=20, fast=True).to(device)
        bald = BALD(eval_fwd_exp(model), device=device, batch_size=512, **kwargs)
        dm = DataManager(train, pool, bald)
        dm.reset()  # to reset pool
        print(f"=== repeat #{r} of {REPS} ===")
        for i in range(1, ITERS + 1):
            model.reset_weights()
            # since we're collecting calibration metrics,
            # make pool return targets too. (i.e. debug mode)
            with dm.unlabelled.tmp_debug():
                trainer = EphemeralTrainer(
                    model,
                    dm.unlabelled,
                    F.nll_loss,
                    "Adam",
                    threshold=threshold,
                    min_labelled=0.1,
                    log_dir=(calib_metrics / "SSL" / f"rep_{r}" / f"iter_{i}"),
                    patience=(3, 7),
                    reload_best=True,
                    device=device,
                    pool_loader_kwargs=kwargs,
                )
                train_loader = torchdata.DataLoader(
                    dm.labelled,
                    batch_size=BATCH_SIZE,
                    sampler=RandomFixedLengthSampler(
                        dm.labelled, MIN_TRAIN_LEN, shuffle=True
                    ),
                    **kwargs,
                )
                with timeop() as t:
                    history = trainer.fit(
                        train_loader,
                        val_loader,
                        iterations=SSL_ITERATIONS,
                        epochs=EPOCHS,
                    )
            # eval on test set
            test_metrics = trainer.evaluate(test_loader)
            accs[dm.n_labelled].append(test_metrics["acc"])
            print(f"-- Iteration {i} of {ITERS} --")
            print(
                f"\ttrain: {dm.n_labelled}; pool: {dm.n_unlabelled}\n"
                f"\t[test] acc: {test_metrics['acc']}; time: {t}"
            )

            # save stuff
            with open(metrics / f"rep_{r}_iter_{i}.pkl", "wb") as fp:
                payload = {
                    "history": history,
                    "test_metrics": test_metrics,
                    "labelled_classes": dm.unlabelled.labelled_classes,
                    "labelled_indices": dm.unlabelled.labelled_indices,
                }
                pickle.dump(payload, fp)
            torch.save(model.state_dict(), saved_models / f"rep_{r}_iter_{i}.pth")

            with open(f"{template}_accs.pkl", "wb") as fp:
                pickle.dump(accs, fp)

            # there won't be anther iteration, don't have to acquire.
            if i == ITERS:
                continue

            # -- before acquiring, reset weights and train using supervised .
            model.reset_weights()
            supervised_trainer = Trainer(
                model,
                F.nll_loss,
                "Adam",
                patience=3,
                reload_best=True,
                device=device,
            )
            train_loader = torchdata.DataLoader(
                dm.labelled,
                batch_size=BATCH_SIZE,
                sampler=RandomFixedLengthSampler(
                    dm.labelled, MIN_TRAIN_LEN, shuffle=True
                ),
                **kwargs,
            )

            save_pl_metrics = create_supervised_evaluator(
                model, metrics=None, device=device
            )
            pps = PLPredictionSaver(
                log_dir=(calib_metrics / "supervised" / f"rep_{r}" / f"iter_{i}")
            )
            pps.attach(save_pl_metrics)

            def _save_pl_metrics(e):
                pps.global_step_from_engine(e)
                save_pl_metrics.run(
                    torchdata.DataLoader(
                        dm.unlabelled, batch_size=512, shuffle=False, **kwargs
                    )
                )

            callbacks = [_save_pl_metrics]
            with dm.unlabelled.tmp_debug():
                supervised_trainer.fit(
                    train_loader, val_loader, epochs=EPOCHS, callbacks=callbacks
                )

            # finally, acquire points
            dm.acquire(b)


if __name__ == "__main__":
    main(threshold=0.90, b=10)
