from alr.training.pl_mixup import PLMixupTrainer, temp_ds_transform
from alr.utils import manual_seed, timeop
from alr.data.datasets import Dataset
from alr.data import DataManager, UnlabelledDataset
from alr.training.utils import PLPredictionSaver
from alr import ALRModel
from alr.acquisition import AcquisitionFunction

import torch
import pickle
import torch.utils.data as torchdata
import torchvision as tv
from collections import defaultdict
from ignite.engine import create_supervised_evaluator
from pathlib import Path
from torch import nn
import numpy as np


def make_imbalanced_pool(dataset: torchdata.Dataset, classes, n):
    idxs = []
    count = {c: n for c in classes}
    for idx in np.random.permutation(len(dataset)):
        y = dataset[idx][1]
        if y in count:
            if count[y] > 0:
                idxs.append(idx)
                count[y] -= 1
        else:
            idxs.append(idx)
    return torchdata.Subset(dataset, idxs)


def uneven_split(dataset: torchdata.Dataset, mapping: dict) -> tuple:
    count = {k: v for k, v in mapping.items()}
    original_idxs = set(range(len(dataset)))
    idxs = []
    for idx in np.random.permutation(len(dataset)):
        if all(i == 0 for i in count.values()):
            break
        y = dataset[idx][1]
        if count[y]:
            count[y] -= 1
            idxs.append(idx)
    return torchdata.Subset(dataset, idxs), torchdata.Subset(
        dataset, list(original_idxs - set(idxs))
    )


class LastKL(AcquisitionFunction):
    def __init__(self):
        super(LastKL, self).__init__()
        self.labels_E_N_C: torch.Tensor = None
        self.recent_score = None

    def __call__(self, X_pool: torchdata.Dataset, b: int):
        pool_size = len(X_pool)
        mc_preds = self.labels_E_N_C.double()
        E, N, C = mc_preds.shape
        # p = [N, C]
        p = mc_preds[-1]
        # reverse "mode-seeking" kl divergence
        scores = LastKL._kl_divergence(mc_preds[:-1], p.unsqueeze(0)).mean(0)
        assert torch.isfinite(scores).all()
        assert scores.shape == (pool_size,)
        result = torch.argsort(scores, descending=True).numpy()
        self.recent_score = scores.numpy()
        return result[:b]

    @staticmethod
    def _kl_divergence(p, q):
        return (p * torch.log(p / (q + 1e-5) + 1e-5)).sum(dim=-1)


class Net(ALRModel):
    def __init__(self, model):
        super(Net, self).__init__()
        self.model = model
        self.snap()

    def forward(self, x):
        return self.model(x)


def calc_calib_metrics(loader, model: nn.Module, log_dir, device):
    evaluator = create_supervised_evaluator(model, metrics=None, device=device)
    pds = PLPredictionSaver(log_dir)
    pds.attach(evaluator)
    evaluator.run(loader)


def main(seed: int, alpha: float, b: int, augment: bool, iters: int, repeats: int):
    acq_name = "lastKL"
    print(f"Starting experiment with seed {seed}, augment = {augment} ...")
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    kwargs = dict(num_workers=4, pin_memory=True)

    # ========= CONSTANTS ===========
    BATCH_SIZE = 100
    # at least have this much points in one epoch (see RandomFixedLengthSampler)
    MIN_TRAIN_LENGTH = 20_000
    VAL_SIZE = 5_000
    MIN_LABELLED = 16
    # stage 1 and stage 2 patience
    PATIENCE = (5, 25)
    # how many epochs before LR is reduced
    LR_PATIENCE = 10
    # stage 1 and stage 2 of PL mixup training
    EPOCHS = (100, 400)

    REPEATS = repeats
    ITERS = iters

    # ========= SETUP ===========
    manual_seed(42)
    mapping = {cls: 110 for cls in range(10)}
    mapping[0] = 10
    mapping[4] = 10
    mapping[9] = 10
    train, test = Dataset.CIFAR10.get(raw=True)
    train, pool = uneven_split(train, mapping)
    pool_idxs = pool.indices[:]
    class_count = defaultdict(int)
    for _, y in train:
        class_count[y] += 1
    print("Initial dataset class count:")
    print(class_count)

    pool, val = torchdata.random_split(pool, (len(pool) - VAL_SIZE, VAL_SIZE))
    pool_idxs = (pool_idxs, pool.indices)

    pool = make_imbalanced_pool(pool, [0, 4, 9], n=500)
    pool_idxs = (*pool_idxs, pool.indices)
    class_count = defaultdict(int)
    for _, y in pool:
        class_count[y] += 1
    print("Unlabelled class count:")
    print(class_count)

    manual_seed(seed)
    pool = UnlabelledDataset(pool)
    test_loader = torchdata.DataLoader(
        test,
        batch_size=512,
        shuffle=False,
        **kwargs,
    )
    train_transform = test_transform = tv.transforms.Compose(
        [
            tv.transforms.ToTensor(),
            tv.transforms.Normalize(*Dataset.CIFAR10.normalisation_params),
        ]
    )
    test_ds_transform = temp_ds_transform(test_transform)
    if augment:
        data_augmentation = Dataset.CIFAR10.get_augmentation
    else:
        data_augmentation = None
    accs = defaultdict(list)

    template = (
        f"{acq_name}_{b}_alpha_{alpha}" + ("_aug" if augment else "") + f"_{seed}"
    )
    metrics = Path("metrics") / template
    calib_metrics = Path("calib_metrics") / template
    saved_models = Path("saved_models") / template
    metrics.mkdir(parents=True, exist_ok=True)
    calib_metrics.mkdir(parents=True, exist_ok=True)
    saved_models.mkdir(parents=True, exist_ok=True)
    bald_scores = None

    # since we need to know which points were taken for val dataset
    with open(metrics / "pool_idxs.pkl", "wb") as fp:
        pickle.dump(pool_idxs, fp)

    for r in range(1, REPEATS + 1):
        print(f"- [{acq_name} (b={b})] repeat #{r} of {REPEATS}-")
        model = Net(Dataset.CIFAR10.model).to(device)
        acq_fn = LastKL()
        dm = DataManager(train, pool, acq_fn)
        dm.reset()  # this resets pool

        for i in range(1, ITERS + 1):
            model.reset_weights()
            trainer = PLMixupTrainer(
                model,
                "SGD",
                train_transform,
                test_transform,
                {"lr": 0.1, "momentum": 0.9, "weight_decay": 1e-4},
                kwargs,
                log_dir=None,
                rfls_length=MIN_TRAIN_LENGTH,
                alpha=alpha,
                min_labelled=MIN_LABELLED,
                data_augmentation=data_augmentation,
                batch_size=BATCH_SIZE,
                patience=PATIENCE,
                lr_patience=LR_PATIENCE,
                device=device,
            )
            with dm.unlabelled.tmp_debug():
                with timeop() as t:
                    history = trainer.fit(
                        dm.labelled, val, dm.unlabelled, epochs=EPOCHS
                    )

            acq_fn.labels_E_N_C = trainer.soft_label_history

            # eval
            test_metrics = trainer.evaluate(test_loader)
            print(f"=== Iteration {i} of {ITERS} ({i / ITERS:.2%}) ===")
            print(
                f"\ttrain: {dm.n_labelled}; val: {len(val)}; "
                f"pool: {dm.n_unlabelled}; test: {len(test)}"
            )
            print(f"\t[test] acc: {test_metrics['acc']:.4f}, time: {t}")
            accs[dm.n_labelled].append(test_metrics["acc"])

            # save stuff

            # pool calib
            with dm.unlabelled.tmp_debug():
                pool_loader = torchdata.DataLoader(
                    temp_ds_transform(test_transform, with_targets=True)(dm.unlabelled),
                    batch_size=512,
                    shuffle=False,
                    **kwargs,
                )
                calc_calib_metrics(
                    pool_loader,
                    model,
                    calib_metrics / "pool" / f"rep_{r}" / f"iter_{i}",
                    device=device,
                )
            calc_calib_metrics(
                test_loader,
                model,
                calib_metrics / "test" / f"rep_{r}" / f"iter_{i}",
                device=device,
            )

            with open(metrics / f"rep_{r}_iter_{i}.pkl", "wb") as fp:
                payload = {
                    "history": history,
                    "test_metrics": test_metrics,
                    "labelled_classes": dm.unlabelled.labelled_classes,
                    "labelled_indices": dm.unlabelled.labelled_indices,
                    "bald_scores": bald_scores,
                }
                pickle.dump(payload, fp)
            torch.save(model.state_dict(), saved_models / f"rep_{r}_iter_{i}.pt")
            # flush results frequently for the impatient
            with open(template + "_accs.pkl", "wb") as fp:
                pickle.dump(accs, fp)

            # finally, acquire points
            # transform pool samples toTensor and normalise them (since we used raw above!)
            acquired_idxs, _ = dm.acquire(b=b, transform=test_ds_transform)
            # if bald, store ALL bald scores and the acquired idx so we can map the top b scores
            # to the b acquired_idxs
            # acquired_idxs has the top b scores from recent_score
            bald_scores = (acquired_idxs, acq_fn.recent_score)


if __name__ == "__main__":
    import argparse

    args = argparse.ArgumentParser()
    args.add_argument("--seed", type=int, default=42)
    args.add_argument("--alpha", type=float, default=0.4)
    args.add_argument("--b", default=10, type=int, help="Batch acq size (default = 10)")
    args.add_argument("--augment", action="store_true")
    args.add_argument("--iters", default=199, type=int)
    args.add_argument("--reps", default=1, type=int)
    args = args.parse_args()

    main(
        seed=args.seed,
        alpha=args.alpha,
        b=args.b,
        augment=args.augment,
        iters=args.iters,
        repeats=args.reps,
    )
