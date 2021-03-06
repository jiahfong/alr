from typing import Callable, Sequence, Optional, Tuple

import torch
import torch.utils.data as torchdata
import copy

from alr.acquisition import AcquisitionFunction
from contextlib import contextmanager
import numpy as np
import torchvision as tv


class UnlabelledDataset(torchdata.Dataset):
    def __init__(
        self,
        dataset: torchdata.Dataset,
        label_fn: Optional[Callable[[torchdata.Dataset], torchdata.Dataset]] = None,
        debug: Optional[bool] = False,
    ):
        r"""
        A wrapper class to manage the unlabelled `dataset` by providing a simple
        interface to :meth:`label` specific points and remove from the underlying dataset.
        Note that it doesn't physically remove points from `dataset`, but rather provide
        an abstraction over it to *logically* remove them.
        Furthermore, if the `label_fn` is not provided, this class automatically infers that
        the provided "unlabelled" dataset is, in fact, labelled. This is especially for
        benchmarking studies!

        Args:
            dataset (:class:`torch.utils.data.Dataset`): unlabelled dataset
            label_fn (Callable: Dataset :math:`\rightarrow` Dataset, optional): a function that
                takes an unlabelled dataset and returns another
                dataset that's fully labelled. If this is not provided, then `dataset` should
                be labelled.
            debug (bool, optional): Turn debug mode on. If `True`, then indexing this dataset
                will return both `(x, y)` instead of just `x`; this is useful for research purposes.
                Note, `label_fn` must be `None` otherwise an error will be raised.
        """
        self._dataset = dataset
        self._label_fn = label_fn
        self._mask = torch.ones(len(dataset), dtype=torch.bool)
        self._len = len(dataset)
        self._idx_mask = torch.arange(len(dataset))
        self.debug = debug
        if self.debug:
            assert self._label_fn is None
        # Edited on Sat Jan  2 21:59:40 GMT 2021
        # alias for backwards compatibility.
        self.tmp_debug = self.true_labels

    def label(self, idxs: Sequence[int]) -> torchdata.Dataset:
        r"""
        Label and return points specified by `idxs` according to provided `label_fn`.
        These labelled points will no longer be part of this dataset. Note, however,
        that this is just an abstraction and the original provided dataset in the constructor
        will *not* be modified. In other words, the dataset will not *lose* points
        as a result of being labelled.

        Args:
            idxs (`Sequence[int]`): indices of points to label

        Returns:
            :class:`torch.utils.data.Dataset`: a labelled dataset where each
                                                point is specified by `idxs` and labelled by `label_fn`.
        """
        # indices of data where it hasn't been labelled yet
        local_mask = self._idx_mask

        # can't acquire something that's not in the pool anymore
        assert self._mask[
            local_mask[idxs]
        ].all(), "Can't label points that have been labelled."
        assert self._len, "There are no remaining unlabelled points."
        labelled = torchdata.Subset(self._dataset, local_mask[idxs])
        if self._label_fn:
            labelled = self._label_fn(labelled)

        # update masks and length
        self._mask[local_mask[idxs]] = 0
        self._idx_mask = torch.nonzero(self._mask).flatten()
        self._len -= len(idxs)
        return labelled

    def __getitem__(self, idx) -> torch.Tensor:
        # indices of data where it hasn't been labelled yet
        if self._label_fn or self.debug:
            # user provided x only or debug mode is on, return (x, y)
            return self._dataset[self._idx_mask[idx].item()]
        # user provided (x, y) => return x only
        return self._dataset[self._idx_mask[idx].item()][0]

    def __len__(self) -> int:
        return self._len

    def convert_idx(self, idxs: np.array) -> np.array:
        r"""
        Given a set of indices relative to the current state of UnlabelledDataset,
        return the true/absolute index of the original pool dataset.
        Args:
            idxs (np.array): sequence of indices

        Returns:
            `np.array`: absolute index
        """
        return self._idx_mask[idxs].numpy()

    @property
    def labelled_indices(self) -> list:
        r"""
        Returns a list of indices that were labelled in the past.

        Returns:
            `list`: all the indices that were labelled by :meth:`label`
        """
        return torch.nonzero(~self._mask).flatten().tolist()

    @property
    def labelled_classes(self) -> list:
        r"""
        Return a list of classes that were labelled by the user (`label_fn`).

        Returns:
            list: list of classes
        """
        if self._label_fn is not None:
            import warnings

            # xxx: because it's 2 am and i'm lazy. (note to self: could save these targets in .label() fn)
            warnings.warn(
                "UnlabelledDataset was initialised with label_fn but labelled_classes was invoked."
            )
        classes = []
        for i in self.labelled_indices:
            classes.append(self._dataset[i][1])
        return classes

    def reset(self) -> None:
        r"""
        Reset to initial state -- all labelled points are unlabelled and
        introduced back into the pool.

        Returns:
            NoneType: None
        """
        self._mask = torch.ones(len(self._dataset), dtype=torch.bool)
        self._idx_mask = torch.arange(len(self._dataset))
        self._len = len(self._dataset)

    @contextmanager
    def true_labels(self):
        r"""
        When the dataset is index within this context, it returns the label as well. It's useful for
        debugging/evaluation purposes. This assumes `label_fn` was None -- which indicates that the dataset
        came with labels to begin with. If `label_fn` is not None, then this method does nothing -- the indexed
        datum will only contain features, as usual.

        Returns:
            :class:`UnlabelledDataset`: `self`
        """
        if self.debug:
            yield self
        else:
            self.debug = True
            yield self
            self.debug = False


class DataManager:
    def __init__(
        self,
        labelled: torchdata.Dataset,
        unlabelled: UnlabelledDataset,
        acquisition_fn: AcquisitionFunction,
    ):
        r"""
        A stateful data manager class

        The :attr:`labelled` and :attr:`unlabelled` datasets are updated according to the points
        acquired by :meth:`acquire`. `acquisition_fn` dictates which points should
        be chosen from the unlabelled pool. Similar to :class:`UnlabelledDataset`, the original
        dataset, `labelled`, will *not* be modified as this class provides a *logical* abstraction.
        Whilst `unlabelled` is modified, the dataset that it is providing an abstraction over is not
        (see :class:`UnlabelledDataset`).


        Args:
            labelled (:class:`~torch.utils.data.Dataset`): training data with labelled points
            unlabelled (:class:`UnlabelledDataset`): unlabelled pool
            acquisition_fn (:class:`~alr.acquisition.AcquisitionFunction`): acquisition function
        """
        self._old_labelled = labelled
        self._labelled = labelled
        self._unlabelled = unlabelled
        self._a_fn = acquisition_fn

    def acquire(self, b: int, transform=None) -> Tuple[np.array, torchdata.Dataset]:
        r"""
        Acquire `b` points from the :attr:`unlabelled` dataset and adds
        it to the :attr:`labelled` dataset.

        Args:
            b (int): number of points to acquire at once
            transform (Callable, optional): transform the unlabelled dataset before
                giving it to the acquisition function. The function is expected
                to take and return a dataset.

        Returns:
            `Tuple[np.array, torch.utils.data.Dataset]`: A tuple consisting of
                (1) numpy array with indices that were selected by the acquisition function; and
                (2) Subset-type dataset with the `b` points that were freshly labelled

        Notes:
            the returned numpy array of indices indexes the original pool dataset. I.e., it's the
            "absolute" index relative to the original pool set.
        """
        assert b <= self.n_unlabelled
        if transform is None:
            idxs = self._a_fn(self._unlabelled, b)
        else:
            idxs = self._a_fn(transform(self._unlabelled), b)
        assert idxs.shape == (b,)
        # get true indices (i.e. index from *full* pool set)
        true_idxs = self._unlabelled.convert_idx(idxs)
        # it's important to get true_idxs before calling label(),
        labelled = self._unlabelled.label(idxs)
        self.append_to_labelled(labelled)
        return true_idxs, labelled

    @property
    def n_labelled(self) -> int:
        r"""
        Current number of :attr:`labelled` points.

        Returns:
            int: size of dataset
        """
        return len(self._labelled)

    @property
    def n_unlabelled(self) -> int:
        r"""
        Current number of :attr:`unlabelled` points.

        Returns:
            int: size of dataset
        """
        return len(self._unlabelled)

    @property
    def labelled(self) -> torchdata.Dataset:
        r"""
        The current labelled dataset after considering previous acquisitions.

        Returns:
            :class:`torch.utils.data.Dataset`: labelled dataset
        """
        return self._labelled

    @property
    def unlabelled(self) -> torchdata.Dataset:
        r"""
        The current unlabelled dataset after considering previous acquisitions.

        Returns:
            :class:`torch.utils.data.Dataset`: unlabelled dataset
        """
        return self._unlabelled

    def reset(self) -> None:
        r"""
        Resets the state of this data manager. All acquired points are removed
        from the :attr:`labelled` dataset and added back into the :attr:`unlabelled` dataset.

        Returns:
            NoneType: None
        """
        self._unlabelled.reset()
        self._labelled = self._old_labelled

    def append_to_labelled(self, dataset: torchdata.Dataset):
        r"""
        Logically appends given dataset to the labelled dataset. Again, this does not
        physically modify the provided dataset.

        Args:
            dataset (torch.utils.data.Dataset): dataset object

        Returns:
            NoneType: None
        """
        # TODO(optim): is there a better way to do this?
        self._labelled = torchdata.ConcatDataset((self._labelled, dataset))


class PseudoLabelDataset(torchdata.Dataset):
    def __init__(self, dataset: torchdata.Dataset, pseudo_labels: Sequence):
        r"""
        Provides dataset with pseudo-labels. Dataset's `__getitem__` is expected to
        return x only (i.e. without targets). Use :class:`RelabelDataset` if
        dataset is labelled.

        Args:
            dataset (torch.utils.data.Dataset): dataset object
            pseudo_labels (Sequence): pseudo-labels
        """
        assert len(pseudo_labels) == len(dataset)
        self._dataset = dataset
        self._labels = pseudo_labels

    def __len__(self):
        return len(self._dataset)

    def __getitem__(self, idx):
        return self._dataset[idx], self._labels[idx]


class RelabelDataset(torchdata.Dataset):
    def __init__(self, dataset: torchdata.Dataset, labels: Sequence):
        r"""
        Overrides dataset labels. Dataset's `__getitem__` is expected to
        return (x, y) (i.e. with targets). Use :class:`PseudoLabelDataset` if
        dataset in unlabelled.

        Args:
            dataset (torch.utils.data.Dataset): dataset object
            labels (Sequence): new labels
        """
        assert len(labels) == len(dataset)
        self._dataset = dataset
        self._labels = labels

    def __len__(self):
        return len(self._dataset)

    def __getitem__(self, idx):
        return self._dataset[idx][0], self._labels[idx]


class TransformedDataset(torchdata.Dataset):
    """
    Transforms and augments an untransformed and unaugmented dataset.
    """

    def __init__(
        self,
        raw_dataset: torchdata.Dataset,
        transform: Optional[list] = None,
        augmentation: Optional[list] = None,
    ):
        self.raw_dataset = raw_dataset
        self._transforms = transform
        self._augmentations = augmentation
        self.transform = tv.transforms.Compose(transform) if transform else lambda x: x
        self.augmentation = (
            tv.transforms.Compose(augmentation) if augmentation else lambda x: x
        )

    def __len__(self):
        return len(self.raw_dataset)

    def __getitem__(self, idx):
        item = self.raw_dataset[idx]
        if isinstance(item, (list, tuple)):
            x, y = item
            return self.transform(self.augmentation(x)), y
        else:
            return self.transform(self.augmentation(item))


def disable_augmentation(dataset: UnlabelledDataset):
    r"""
    When using :class:`UnlabelledDataset` and :class:`TransformedDataset`, this function
    can be used in :meth:`UnlabelledDataset.acquire`'s transform argument to disable augmentation
    before scoring with an acquisition function (without modifying `dataset`).

    Args:
        dataset (UnlabelledDataset): unlabelled dataset to transform. Assumes `dataset._dataset` is
            of type :class:`TransformedDataset`.

    Returns:
        UnlabelledDataset: a new transformed unlabelled dataset that has no augmentation,
            leaving the original unlabelled dataset (`dataset`) untouched.
    """
    expected_len = len(dataset)
    ds = dataset._dataset
    assert isinstance(ds, TransformedDataset)
    new_dataset = copy.copy(dataset)
    new_dataset._dataset = TransformedDataset(
        ds.raw_dataset, transform=ds._transforms, augmentation=None
    )
    assert len(new_dataset) == expected_len
    return new_dataset
